"""
custom_terms.py — User-configurable custom-term store, matcher, and detector

This module owns the *custom_term* category of the pre-inference redaction
stage. It provides:

  * ``CustomTermsStore`` — a thread-safe, in-memory store that loads a list of
    user-defined redaction terms from a local JSON config file, validates them,
    keeps a compiled matching regex, persists add/remove mutations atomically,
    and live-reloads when the config file changes on disk (mtime-based).
  * ``CustomTermDetector`` — a thin adapter that implements the ``Detector``
    Protocol from ``redactor`` (``category == "custom_term"``) by delegating to
    a store.

Design references: the "Custom-terms config (custom_terms.py)" and
"Custom-term detector" sections of the pre-inference-redaction design doc, and
Requirements 4.1–4.9 / 5 (add/remove semantics).

Nothing in this module makes a network call. Detection and persistence are
purely local (Non-Goals: nothing leaves the machine).

Custom-term matching is intentionally *literal*: every term is passed through
``re.escape`` so a term containing regex metacharacters (``C++``,
``Project [Omega]``, ``a.b.c``, ``$$$``) matches the literal characters and can
never inject regex behavior into the combined pattern.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading

# Build on the existing redactor primitives; do NOT redefine them here.
# ``Span`` carries only offsets + category, and "custom_term" already exists in
# redactor.CATEGORIES. ``Detector`` is the Protocol our detector satisfies.
from redactor import Detector, Span

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

CATEGORY = "custom_term"

# Default on-disk config location (a JSON array of strings). The store
# constructor accepts a path argument so tests can point at a tmp_path and
# never touch this real file.
_DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "redaction_terms.json")

# Validation bounds (Req 4.1, 4.3).
MAX_TERMS = 10_000
MAX_TERM_LEN = 256


# ---------------------------------------------------------------------------
# CustomTermsStore
# ---------------------------------------------------------------------------

class CustomTermsStore:
    """Thread-safe store of custom redaction terms with live reload.

    Holds an ordered term list, a lowercased set (for O(1) case-insensitive
    dedupe), a compiled alternation regex, the config path, and the last-seen
    config mtime — all guarded by a single ``threading.Lock`` for both reads
    and writes so concurrent generate requests and reloads stay consistent.

    Matching (Req 4.2) is a case-insensitive, boundary-aware, *literal* whole
    occurrence of each term. The combined regex is built once per term-list
    change (load / add / remove / reload), never per ``detect`` call.
    """

    def __init__(self, path: str | None = None):
        self._path = path if path is not None else _DEFAULT_CONFIG_PATH
        self._lock = threading.Lock()
        # State guarded by ``self._lock``.
        self._terms: list[str] = []           # ordered, as-loaded/added
        self._lower_set: set[str] = set()      # lowercased terms for dedupe
        self._regex: re.Pattern[str] | None = None  # None => empty list, no catch-all
        self._mtime: float | None = None       # last-seen config mtime
        # Diagnostics: each skipped/failed term or load recorded with a reason.
        self._skipped: list[dict[str, str]] = []

        # Initial load from disk.
        with self._lock:
            self._load_locked()

    # -- public API --------------------------------------------------------

    def terms(self) -> list[str]:
        """Return a snapshot copy of the current term list (under the lock)."""
        with self._lock:
            return list(self._terms)

    def skipped(self) -> list[dict[str, str]]:
        """Return a snapshot of skip/failure records (term + reason)."""
        with self._lock:
            return list(self._skipped)

    def detect(self, text: str) -> list[Span]:
        """Return ``custom_term`` spans for every term occurrence in ``text``.

        Performs a cheap mtime check first (live reload, Req 4.6) so a config
        change on disk is picked up without a restart, then matches under the
        lock against the compiled regex. An empty term list means no regex was
        built, so this returns ``[]`` without scanning (Req 4.2 vacuous case).
        """
        self.maybe_reload()
        with self._lock:
            if self._regex is None:
                return []
            return [Span(m.start(), m.end(), CATEGORY) for m in self._regex.finditer(text)]

    def maybe_reload(self) -> bool:
        """Reload the term list from disk *iff* the config mtime changed.

        Cheap by design: a single ``os.stat`` mtime read on the common path
        where nothing changed; the full read + validate + regex rebuild happens
        only when the mtime differs. Returns True if a reload occurred.

        Handles the file being deleted between checks gracefully: a missing
        file reloads to an empty list (and records the reason) rather than
        raising.
        """
        try:
            current_mtime = os.stat(self._path).st_mtime
        except OSError:
            # File missing/unreadable now. If we previously had a live file,
            # reload to an empty list; otherwise nothing to do.
            with self._lock:
                if self._mtime is None:
                    return False
                self._load_locked()
                return True

        with self._lock:
            if self._mtime is not None and current_mtime == self._mtime:
                return False  # unchanged — skip the expensive reload
            self._load_locked()
            return True

    def add(self, term: str) -> list[str]:
        """Add ``term`` (trimmed) and persist atomically; return updated list.

        - Trims leading/trailing whitespace (Req 5.2).
        - Rejects empty-after-trim or >256 chars with ``ValueError`` (Req 5.5).
        - Case-insensitive dedupe: an existing term is a no-op leaving the list
          unchanged and returns the current list (Req 5.3), without a rewrite.
        - On a real change, mutates under the lock then persists atomically
          (Req 4.7). If persistence fails the in-memory change is rolled back
          so the in-memory list matches the untouched on-disk file (Req 4.8),
          and the error is surfaced.
        """
        normalized = term.strip()
        if not normalized:
            raise ValueError("empty term")
        if len(normalized) > MAX_TERM_LEN:
            raise ValueError("term exceeds 256 chars")

        with self._lock:
            if normalized.lower() in self._lower_set:
                return list(self._terms)  # duplicate: no-op, no rewrite

            # Snapshot for rollback on persistence failure.
            prev_terms = list(self._terms)
            prev_lower = set(self._lower_set)
            prev_regex = self._regex

            self._terms.append(normalized)
            self._lower_set.add(normalized.lower())
            self._rebuild_regex_locked()
            try:
                self._persist_locked()
            except Exception:
                # Retain prior in-memory list + leave prior file bytes
                # unchanged (Req 4.8); surface the error to the caller.
                self._terms = prev_terms
                self._lower_set = prev_lower
                self._regex = prev_regex
                raise
            return list(self._terms)

    def remove(self, term: str) -> tuple[list[str], bool]:
        """Remove ``term`` case-insensitively; return (updated list, removed).

        Absent term is a no-op returning ``removed=False`` and the unchanged
        list (Req 5.6). On a real change, mutates under the lock then persists
        atomically; on persistence failure the change is rolled back and the
        error surfaced (Req 4.8).
        """
        target = term.strip().lower()
        with self._lock:
            if target not in self._lower_set:
                return list(self._terms), False

            prev_terms = list(self._terms)
            prev_lower = set(self._lower_set)
            prev_regex = self._regex

            self._terms = [t for t in self._terms if t.lower() != target]
            self._lower_set.discard(target)
            self._rebuild_regex_locked()
            try:
                self._persist_locked()
            except Exception:
                self._terms = prev_terms
                self._lower_set = prev_lower
                self._regex = prev_regex
                raise
            return list(self._terms), True

    # -- internal helpers (all assume the lock is held) --------------------

    def _load_locked(self) -> None:
        """(Re)load and validate terms from disk, rebuilding derived state.

        Missing / unreadable / invalid-JSON config => empty list + a recorded
        reason, never a crash (Req 4.9). Otherwise validate each term (Req 4.1,
        4.3): trim, skip empty/whitespace-only, skip >256 chars, skip
        case-insensitive duplicates, cap at 10,000; every skip is recorded with
        a reason (logged + retained in ``self._skipped``). Refreshes the cached
        mtime so ``maybe_reload`` can short-circuit next time.
        """
        self._skipped = []
        raw: object

        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            # Refresh mtime from the handle's file after a successful read.
            self._mtime = os.stat(self._path).st_mtime
        except FileNotFoundError:
            self._set_empty_locked(reason="config file not found")
            self._mtime = None
            return
        except OSError as err:
            self._set_empty_locked(reason=f"config file unreadable: {err}")
            self._mtime = None
            return
        except (json.JSONDecodeError, ValueError) as err:
            self._set_empty_locked(reason=f"config file is not valid JSON: {err}")
            # Keep the mtime so we don't re-read an unchanged broken file every
            # request; a fix changes the mtime and triggers a reload.
            try:
                self._mtime = os.stat(self._path).st_mtime
            except OSError:
                self._mtime = None
            return

        if not isinstance(raw, list):
            self._set_empty_locked(reason="config root is not a JSON array")
            return

        loaded: list[str] = []
        lower_set: set[str] = set()
        for entry in raw:
            if len(loaded) >= MAX_TERMS:
                self._record_skip(str(entry), f"term cap of {MAX_TERMS} reached")
                continue
            if not isinstance(entry, str):
                self._record_skip(repr(entry), "term is not a string")
                continue
            normalized = entry.strip()
            if not normalized:
                self._record_skip(entry, "empty or whitespace-only term")
                continue
            if len(normalized) > MAX_TERM_LEN:
                self._record_skip(normalized, "term exceeds 256 chars")
                continue
            lowered = normalized.lower()
            if lowered in lower_set:
                self._record_skip(normalized, "case-insensitive duplicate term")
                continue
            loaded.append(normalized)
            lower_set.add(lowered)

        self._terms = loaded
        self._lower_set = lower_set
        self._rebuild_regex_locked()

    def _set_empty_locked(self, reason: str) -> None:
        """Reset to an empty term list and record the reason (Req 4.9)."""
        self._terms = []
        self._lower_set = set()
        self._regex = None
        self._record_skip(term="", reason=reason)

    def _record_skip(self, term: str, reason: str) -> None:
        """Record and log a skipped term / load issue with its reason."""
        self._skipped.append({"term": term, "reason": reason})
        logger.info("custom_terms: skipped term %r: %s", term, reason)

    def _rebuild_regex_locked(self) -> None:
        """Compile one combined alternation regex from the current terms.

        Empty term list => no catch-all regex (``self._regex = None``) so
        ``detect`` short-circuits and can never match everything.

        Each term is passed through ``re.escape`` so regex metacharacters are
        matched *literally* and cannot inject pattern behavior. Terms are sorted
        longest-first so a longer term wins over a shorter overlapping one in
        the alternation. Matching is case-insensitive (Req 4.2).

        Boundary rationale: a naive ``\\b...\\b`` wrap is wrong for terms that
        start or end with a non-word character (e.g. ``C++`` ends in ``+``, so
        a trailing ``\\b`` would never match). Instead we attach per-term
        boundaries conditionally: a left ``(?<!\\w)`` only when the term starts
        with a word char, and a right ``(?!\\w)`` only when the term ends with a
        word char. So ``Acme`` won't match inside ``Acmelike`` (word-char ends
        need a non-word boundary), while ``C++`` still matches in ``use C++
        today`` and ``C++.`` (no trailing word-char, so no trailing boundary is
        required).
        """
        if not self._terms:
            self._regex = None
            return

        # Longest-first so longer terms win over shorter overlapping ones.
        ordered = sorted(self._terms, key=len, reverse=True)
        alternatives: list[str] = []
        for term in ordered:
            escaped = re.escape(term)
            left = r"(?<!\w)" if term[:1].isalnum() or term[:1] == "_" else ""
            right = r"(?!\w)" if term[-1:].isalnum() or term[-1:] == "_" else ""
            alternatives.append(f"{left}{escaped}{right}")

        pattern = "|".join(alternatives)
        self._regex = re.compile(pattern, re.IGNORECASE)

    def _persist_locked(self) -> None:
        """Atomically write the current term list to the config file.

        Crash-safe: a temp file in the *same directory* as the config is
        written, flushed, and ``fsync``'d, then swapped into place with
        ``os.replace`` (an atomic rename on the same filesystem). This ensures a
        crash mid-write can never leave a corrupt / half-written active config —
        readers see either the old file or the fully-written new one. The temp
        file is removed on any error before the swap. On success the cached
        mtime is refreshed so ``maybe_reload`` won't treat our own write as an
        external change.
        """
        config_dir = os.path.dirname(os.path.abspath(self._path))
        payload = json.dumps(self._terms, ensure_ascii=False, indent=2)

        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=config_dir,
            prefix=".redaction_terms.",
            suffix=".tmp",
        )
        tmp_path = tmp.name
        try:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp_path, self._path)
        except Exception:
            # Clean up the temp file so a failed write never leaves debris and
            # never corrupts the active config (the swap did not happen).
            try:
                tmp.close()
            except Exception:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        try:
            self._mtime = os.stat(self._path).st_mtime
        except OSError:
            self._mtime = None


# ---------------------------------------------------------------------------
# CustomTermDetector — implements the redactor.Detector Protocol
# ---------------------------------------------------------------------------

class CustomTermDetector:
    """Detector adapter delegating to a ``CustomTermsStore`` (Req 4.2).

    ``category == "custom_term"``. ``detect`` is safe under concurrent reload
    because the store guards all state (term list, regex, mtime) with a lock and
    performs its live-reload check internally.
    """

    category: str = CATEGORY

    def __init__(self, store: CustomTermsStore):
        self._store = store

    def detect(self, text: str) -> list[Span]:
        return self._store.detect(text)


# Static Protocol conformance check (documentation + safety; no runtime cost
# beyond import). Ensures CustomTermDetector satisfies the Detector interface.
_: type[Detector] = CustomTermDetector
