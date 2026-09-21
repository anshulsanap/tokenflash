"""
audit_log.py — Append-only redaction audit log (Req 6)

This module records one entry per redacted span to a local JSONL file. Its two
guarantees are:

  * **Append-only.** Every ``append(...)`` opens the log in append mode ('a')
    and writes exactly one complete JSON object per line terminated by '\\n',
    then flushes. Previously written lines are never rewritten, truncated, or
    reordered (Req 6.2, 6.4). The class exposes ONLY an ``append`` method (plus
    the constructor and a read-only ``path`` property): there is deliberately
    no clear/delete/truncate/overwrite/rotate method, and no code path opens
    the file in a truncating mode.

  * **Zero raw value.** An entry stores ONLY metadata — an ISO-8601
    ``timestamp``, the ``session_id``, the ``category``, its ``placeholder``
    token, and the integer ``length`` of the redacted string. The raw
    sensitive value never enters this module: ``append`` takes the length as an
    ``int``, so no substring of the secret can ever be written. This trivially
    satisfies Req 6.3 (no ≥4-char substring of the raw value appears anywhere
    in an entry).

Concurrency is handled with a ``threading.Lock`` around the file write so
concurrent appends can never interleave partial JSON lines.

Failure handling (Req 6.5): if a write raises (e.g. an unwritable path), the
exception is caught inside ``append``, a WARNING is logged via the stdlib
``logging`` module (there is no raw value to expose), and ``append`` returns
``False`` so the redaction flow continues uninterrupted. A successful append
returns ``True``.

Nothing in this module makes any network call.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone

from redactor import PLACEHOLDERS

logger = logging.getLogger(__name__)

# Default local path for the append-only audit log.
DEFAULT_AUDIT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "redaction_audit.jsonl")


class AuditLog:
    """Append-only JSONL audit log writer (Req 6).

    Public surface is intentionally minimal: construct with an optional path,
    read ``path``, and call ``append(...)``. There is NO method to clear,
    delete, truncate, overwrite, or rotate the log — the file is only ever
    opened in append mode.
    """

    def __init__(self, path: str = DEFAULT_AUDIT_PATH):
        self._path = path
        # Guards the file write so concurrent appends never interleave
        # partial JSON lines (Req 6.4 / Property 11 concurrency).
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        """Read-only path to the append-only log file."""
        return self._path

    def append(
        self,
        category: str,
        session_id: str,
        *,
        length: int,
        placeholder: str | None = None,
    ) -> bool:
        """Append one audit entry for a single redacted span.

        Parameters
        ----------
        category:
            The redaction category (e.g. ``"credit_card"``, ``"person"``) —
            the canonical lowercase value used throughout the codebase.
        session_id:
            The chat session id the redaction belongs to.
        length:
            The character length of the redacted (raw) string. ONLY the integer
            length is accepted and stored; the raw value never enters this
            module, so no substring of it can leak (Req 6.3).
        placeholder:
            The category's placeholder token. When omitted it is derived from
            ``redactor.PLACEHOLDERS[category]``. The placeholder contains no
            raw data.

        Returns
        -------
        bool
            ``True`` when the entry was written and flushed; ``False`` when the
            append failed (the failure is logged as a WARNING and swallowed so
            the redaction flow continues — Req 6.5).
        """
        if placeholder is None:
            # Derive the safe placeholder token from the category. Unknown
            # categories raise KeyError here BEFORE any file work; that is a
            # programming error in the caller, not a runtime audit failure.
            placeholder = PLACEHOLDERS[category]

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "category": category,
            "placeholder": placeholder,
            "length": int(length),
        }
        # Serialize outside the lock; ensure_ascii=False keeps the placeholder's
        # angle-bracket delimiters intact. One complete object, no embedded
        # newlines, terminated by a single '\n'.
        line = json.dumps(entry, ensure_ascii=False) + "\n"

        try:
            with self._lock:
                # Append mode never truncates or rewrites prior lines.
                with open(self._path, "a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
            return True
        except Exception as err:  # noqa: BLE001 — never crash the caller (Req 6.5)
            # No raw value is present in this module, so nothing sensitive is
            # exposed by logging the failure.
            logger.warning(
                "Audit entry could not be written for category %r: %s",
                category,
                err,
            )
            return False
