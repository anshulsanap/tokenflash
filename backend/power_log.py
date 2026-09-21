"""
power_log.py — Append-only per-request power/energy log (Req 9)

This module records one entry per attributed generate request to a local JSONL
file, mirroring ``cache_log.py`` exactly. Its two guarantees:

  * **Append-only.** Every ``append(...)`` opens the log in append mode ('a')
    and writes exactly one complete JSON object per line terminated by '\\n',
    then flushes. Previously written lines are never rewritten, truncated, or
    reordered (Req 9.2). The class exposes ONLY an ``append`` method (plus the
    constructor and a read-only ``path`` property): there is deliberately no
    clear/delete/truncate/overwrite/rotate method.

  * **Zero raw value.** An entry stores ONLY scalars + metadata — an ISO-8601
    ``timestamp``, the ``session_id``, the ``avg_power_watts``, the
    ``energy_joules``, the ``source`` identity, and the ``quality`` flag. The
    raw prompt, redacted-prompt text, and any sensitive value NEVER enter this
    module (Req 9.3).

When the per-request result is ``unavailable``, ``avg_power_watts`` and
``energy_joules`` are written as JSON ``null`` (from ``None``), NEVER ``0``
(Req 3.8, 9.1) — so a genuine measured ``0`` watts stays distinguishable.

Concurrency is handled with a ``threading.Lock`` around the file write. The
JSON line is serialized OUTSIDE the lock; only the write + flush happen under
it. Failure handling (Req 9.4): a write that raises is caught, logged as a
WARNING, and ``append`` returns ``False`` so the request flow continues.

Design reference: the "Components and Interfaces → power_log.py" section of the
hardware-power-tracking design document. Nothing here makes a network call.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Default local path for the append-only power log.
DEFAULT_POWER_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "power_log.jsonl"
)


class PowerLog:
    """Append-only JSONL power log writer (Req 9).

    Public surface is intentionally minimal: construct with an optional path,
    read ``path``, and call ``append(...)``. There is NO method to clear,
    delete, truncate, overwrite, or rotate the log — the file is only ever
    opened in append mode.
    """

    def __init__(self, path: str = DEFAULT_POWER_LOG_PATH):
        self._path = path
        # Guards the file write so concurrent appends never interleave
        # partial JSON lines (Req 9.2 append-only integrity).
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        """Read-only path to the append-only log file."""
        return self._path

    def append(
        self,
        session_id: str,
        *,
        avg_power_watts: float | None,
        energy_joules: float | None,
        source: str,
        quality: str,
    ) -> bool:
        """Append one per-request power entry.

        Parameters
        ----------
        session_id:
            The chat session id the entry belongs to.
        avg_power_watts:
            The attributed Average_Power in watts, or ``None`` when unavailable
            (written as JSON ``null``, never ``0`` — Req 3.8, 9.1).
        energy_joules:
            The attributed Energy_Joules, or ``None`` when unavailable.
        source:
            The active Power_Source identity.
        quality:
            The honesty flag: ``measured`` | ``estimated`` | ``unavailable``.

        Returns
        -------
        bool
            ``True`` when written and flushed; ``False`` when the append failed
            (logged as a WARNING and swallowed so the request continues — Req 9.4).

        Notes
        -----
        The entry carries ONLY scalars + metadata. No raw summary, raw prompt,
        redacted-prompt text, or sensitive value is ever written (Req 9.3).
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            # None serializes to JSON null — never a fabricated 0 (Req 3.8, 9.1).
            "avg_power_watts": (
                None if avg_power_watts is None else float(avg_power_watts)
            ),
            "energy_joules": (
                None if energy_joules is None else float(energy_joules)
            ),
            "source": source,
            "quality": quality,
        }
        # Serialize outside the lock. One complete object, terminated by '\n'.
        line = json.dumps(entry, ensure_ascii=False) + "\n"

        try:
            with self._lock:
                # Append mode never truncates or rewrites prior lines (Req 9.2).
                with open(self._path, "a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
            return True
        except Exception as err:  # noqa: BLE001 — never crash the caller (Req 9.4)
            # No raw value is present in this module, so nothing sensitive is
            # exposed by logging the failure.
            logger.warning(
                "Power log entry could not be written for session %r: %s",
                session_id,
                err,
            )
            return False
