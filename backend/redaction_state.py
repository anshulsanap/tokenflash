"""
redaction_state.py — In-memory toggle flag + per-session cumulative counts

This module holds the runtime state that backs two things:

  * The master ON/OFF toggle for the pre-inference redaction stage
    (``enabled``, default ``True`` per Req 8.6).
  * The per-session cumulative redaction counts that feed the dashboard's
    "Redaction Report" panel via the ``redaction_report`` data annotation
    (``session_counts``: session_id -> {category -> cumulative count}, Req 7.2).
  * The ``ner_available`` flag surfaced for the NER graceful-fallback path
    (Req 3.5); startup wiring (task 11.5) flips it to ``True`` once the local
    NER model loads.

The state is deliberately **in-memory only, lightweight, and JSON-serializable**.
It is never persisted to disk — session counts live only for the life of the
process. Snapshot accessors (``report_for``, ``counts_for``, ``snapshot``)
return plain ``dict``/``int`` structures that ``json.dumps`` accepts directly,
so the ``report_for`` payload can be streamed straight to the React frontend to
populate the Redaction Report panel without any further conversion.

All shared-state reads and writes are guarded by a ``threading.Lock`` so
concurrent generate requests stay consistent, and every returned dict is a
fresh copy (a snapshot) — never a live reference to the internal state — so a
caller can ``json.dumps`` a result without racing an in-flight mutation.

Design reference: the "Toggle & session state (redaction_state.py)" section and
the ``redaction_report`` annotation contract of the pre-inference-redaction
design document.

Nothing in this module performs any network call.
"""

from __future__ import annotations

import threading
from typing import Mapping

# Import the canonical category list rather than hardcoding it, so this module
# stays in lock-step with the redactor's single source of truth (redactor.py).
# It is currently unused for validation (record accepts any category string, so
# a future category needs no change here) but is re-exported for callers that
# want to iterate the known categories when building a benchmark payload.
from redactor import CATEGORIES

__all__ = ["RedactionState", "state", "CATEGORIES"]


class RedactionState:
    """Runtime redaction state: master toggle + per-session cumulative counts.

    Instantiable so tests can create isolated instances; a shared module-level
    singleton (``state``) is also exposed for the FastAPI app, which imports one
    process-wide instance.

    Thread-safety: every read and write of ``enabled``, ``session_counts``, and
    ``ner_available`` happens under ``self._lock``. Accessor methods return
    copies (snapshots) so callers hold no live reference to the internal maps.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Master toggle — default enabled (Req 8.6).
        self._enabled: bool = True
        # session_id -> {category -> cumulative count} (Req 7.2). In-memory only.
        self._session_counts: dict[str, dict[str, int]] = {}
        # NER availability flag; set True once the local model loads (Req 3.5).
        self._ner_available: bool = False

    # -- Master toggle (Req 8.4, 8.6) --------------------------------------

    def is_enabled(self) -> bool:
        """Return whether the redaction stage is currently enabled (Req 8.4)."""
        with self._lock:
            return self._enabled

    def set_enabled(self, value: bool) -> None:
        """Set the master toggle flag (Req 8.3 semantics — the flag only).

        Applying the flag at a request boundary is main.py's responsibility;
        this method just records the desired state.
        """
        with self._lock:
            self._enabled = bool(value)

    # -- NER availability (Req 3.5) ----------------------------------------

    @property
    def ner_available(self) -> bool:
        """Return whether the local NER model is loaded and usable (Req 3.5)."""
        with self._lock:
            return self._ner_available

    def set_ner_available(self, value: bool) -> None:
        """Record whether the NER model loaded successfully (Req 3.5)."""
        with self._lock:
            self._ner_available = bool(value)

    # -- Cumulative session counts (Req 7.2) -------------------------------

    def record(self, session_id: str, category: str, n: int = 1) -> None:
        """Add ``n`` to the cumulative count for ``session_id``+``category``.

        Thread-safe. Creates the per-session and per-category entries on first
        use. A non-positive ``n`` is a no-op (never decrements a cumulative
        count — counts are monotonically non-decreasing for the session).
        """
        if n <= 0:
            return
        with self._lock:
            session = self._session_counts.setdefault(session_id, {})
            session[category] = session.get(category, 0) + n

    def record_many(
        self, session_id: str, category_counts: Mapping[str, int]
    ) -> None:
        """Add a batch of per-category counts from one redaction pass (Req 7.2).

        Thread-safe as a single critical section so the whole batch applies
        atomically. Non-positive per-category values are skipped.
        """
        with self._lock:
            session = self._session_counts.setdefault(session_id, {})
            for category, n in category_counts.items():
                if n <= 0:
                    continue
                session[category] = session.get(category, 0) + n

    def report_for(self, session_id: str) -> dict:
        """Return a JSON-serializable ``redaction_report`` snapshot for a session.

        Shape mirrors the design's ``redaction_report`` payload::

            {"counts": {category: n, ...}, "totalRedactions": N}

        ``counts`` includes only categories with a cumulative count >= 1 for
        this session (Req 7.2). An unknown / empty session yields the empty
        state ``{"counts": {}, "totalRedactions": 0}`` (Req 7.2, design
        empty-state). The returned dict is a fresh copy, safe to ``json.dumps``.
        """
        with self._lock:
            session = self._session_counts.get(session_id)
            counts = (
                {cat: n for cat, n in session.items() if n >= 1}
                if session
                else {}
            )
            total = sum(counts.values())
        return {"counts": counts, "totalRedactions": total}

    def counts_for(self, session_id: str) -> dict[str, int]:
        """Return a copy of the raw per-category counts for a session.

        Returns an empty dict for an unknown session. The copy is detached from
        internal state so callers can read or serialize it without racing an
        in-flight mutation.
        """
        with self._lock:
            session = self._session_counts.get(session_id)
            return dict(session) if session else {}

    def snapshot(self) -> dict[str, dict[str, int]]:
        """Return a deep-ish copy of all session counts (JSON-serializable).

        Cheap full snapshot: session_id -> {category -> count}. Each inner dict
        is copied so the result shares no mutable structure with internal state.
        """
        with self._lock:
            return {sid: dict(counts) for sid, counts in self._session_counts.items()}

    def reset_session(self, session_id: str) -> None:
        """Drop the in-memory counts for a session (test/utility helper).

        Session counts are in-memory only; this simply forgets a session's
        accumulated counts. It does not touch any persisted state (there is
        none) and does not affect the append-only audit log.
        """
        with self._lock:
            self._session_counts.pop(session_id, None)


# Shared, process-wide instance imported by main.py. Tests should construct
# their own ``RedactionState()`` to stay isolated from this singleton.
state = RedactionState()
