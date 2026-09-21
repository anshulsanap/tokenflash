"""
cache_log.py — Append-only semantic-cache decision log (Req 4)

This module records one entry per cache lookup decision to a local JSONL file.
Its two guarantees mirror ``audit_log.py`` exactly:

  * **Append-only.** Every ``append(...)`` opens the log in append mode ('a')
    and writes exactly one complete JSON object per line terminated by '\\n',
    then flushes. Previously written lines are never rewritten, truncated, or
    reordered (Req 4.2, 4.5). The class exposes ONLY an ``append`` method (plus
    the constructor and a read-only ``path`` property): there is deliberately
    no clear/delete/truncate/overwrite/rotate method, and no code path opens
    the file in a truncating mode.

  * **Zero raw value.** An entry stores ONLY scores + metadata — an ISO-8601
    ``timestamp``, the ``session_id``, the ``decision`` ("hit"/"miss"), the
    ``top_score``, the ``runner_up_score`` (the ``0.0`` sentinel when none),
    the ``margin``, and a scalar ``had_runner_up`` bool. The raw requirements
    summary, the raw prompt, the redacted-prompt text, and any sensitive value
    NEVER enter this module — only scores and lightweight metadata are written
    (Req 4.3).

Concurrency is handled with a ``threading.Lock`` around the file write so
concurrent appends can never interleave partial JSON lines. The JSON line is
serialized OUTSIDE the lock; only the write + flush happen under the lock.

Failure handling (Req 4.6): if a write raises (e.g. an unwritable path), the
exception is caught inside ``append``, a WARNING is logged via the stdlib
``logging`` module (there is no raw value to expose), and ``append`` returns
``False`` so the cache flow continues uninterrupted. A successful append
returns ``True``.

Nothing in this module makes any network call.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Default local path for the append-only cache-decision log.
DEFAULT_CACHE_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cache_decisions.jsonl"
)


class CacheLog:
    """Append-only JSONL cache-decision log writer (Req 4).

    Public surface is intentionally minimal: construct with an optional path,
    read ``path``, and call ``append(...)``. There is NO method to clear,
    delete, truncate, overwrite, or rotate the log — the file is only ever
    opened in append mode.
    """

    def __init__(self, path: str = DEFAULT_CACHE_LOG_PATH):
        self._path = path
        # Guards the file write so concurrent appends never interleave
        # partial JSON lines (Req 4.2 append-only integrity).
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        """Read-only path to the append-only log file."""
        return self._path

    def append(
        self,
        session_id: str,
        decision: str,
        *,
        top_score: float,
        runner_up_score: float,
        margin: float,
        had_runner_up: bool,
    ) -> bool:
        """Append one cache-decision entry for a single lookup.

        Parameters
        ----------
        session_id:
            The chat session id the decision belongs to.
        decision:
            The lookup outcome — ``"hit"`` or ``"miss"``.
        top_score:
            The Top_Match Similarity_Score (``0.0`` when no candidate).
        runner_up_score:
            The Runner_Up Similarity_Score; the ``0.0`` sentinel when there is
            no runner-up (Req 4.4).
        margin:
            The Confidence_Margin (``top_score - runner_up_score``).
        had_runner_up:
            Scalar flag making "no runner-up" unambiguous in the log, since a
            genuine ``0.0`` runner-up score is indistinguishable from the
            sentinel otherwise.

        Returns
        -------
        bool
            ``True`` when the entry was written and flushed; ``False`` when the
            append failed (the failure is logged as a WARNING and swallowed so
            the cache flow continues — Req 4.6).

        Notes
        -----
        The entry carries ONLY scores + scalar metadata. No raw summary, raw
        prompt, redacted-prompt text, or sensitive value is ever written
        (Req 4.3).
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "decision": decision,
            "top_score": float(top_score),
            "runner_up_score": float(runner_up_score),
            "margin": float(margin),
            "had_runner_up": bool(had_runner_up),
        }
        # Serialize outside the lock. One complete object, no embedded newlines,
        # terminated by a single '\n'.
        line = json.dumps(entry, ensure_ascii=False) + "\n"

        try:
            with self._lock:
                # Append mode never truncates or rewrites prior lines (Req 4.5).
                with open(self._path, "a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
            return True
        except Exception as err:  # noqa: BLE001 — never crash the caller (Req 4.6)
            # No raw value is present in this module, so nothing sensitive is
            # exposed by logging the failure.
            logger.warning(
                "Cache decision entry could not be written for session %r: %s",
                session_id,
                err,
            )
            return False
