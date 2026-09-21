"""
power_state.py — In-memory toggle + detected-source identity + authorization

This module holds the runtime state that backs the hardware-power-tracking
stage, mirroring ``cache_state.py``:

  * The master ON/OFF toggle for the power stage (``enabled``, default ``True``
    per Req 7.6).
  * The detected active Power_Source identity (``source_name``) and whether the
    MEASURED tier is authorized (``measured_authorized``). Both are set ONCE by
    the lifespan loader via ``set_source(...)`` and are NOT user-toggleable —
    only the on/off flag is, mirroring ``cache_state.cache_available`` vs
    ``enabled``.

The state is deliberately **in-memory only, lightweight, and JSON-serializable**
(never persisted to disk). All shared-state reads and writes are guarded by a
``threading.Lock`` so concurrent generate requests stay consistent.

Design reference: the "Components and Interfaces → power_state.py" section of
the hardware-power-tracking design document. Nothing here makes a network call.
"""

from __future__ import annotations

import threading

__all__ = ["PowerState", "state"]


class PowerState:
    """Runtime power state: master toggle + detected-source identity + auth flag.

    Instantiable so tests can create isolated instances; a shared module-level
    singleton (``state``) is also exposed for the FastAPI app.

    Thread-safety: every read and write happens under ``self._lock``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Master toggle — default enabled (Req 7.6).
        self._enabled: bool = True
        # Set once by the lifespan loader; not user-toggleable.
        self._source_name: str = "unknown"
        self._measured_authorized: bool = False

    # -- Master toggle (Req 7.1, 7.2, 7.6) ---------------------------------

    def is_enabled(self) -> bool:
        """Return whether the power stage toggle is currently on (Req 7.1)."""
        with self._lock:
            return self._enabled

    def set_enabled(self, value: bool) -> None:
        """Set the master toggle flag (Req 7.2 — the flag only).

        Applying the flag at a request boundary is main.py's responsibility;
        this method just records the desired state.
        """
        with self._lock:
            self._enabled = bool(value)

    # -- Detected source identity + authorization (Req 2.2) ----------------

    @property
    def source_name(self) -> str:
        """The identity of the detected active Power_Source."""
        with self._lock:
            return self._source_name

    @property
    def measured_authorized(self) -> bool:
        """Whether a Privileged_Source (measured tier) was selected at startup."""
        with self._lock:
            return self._measured_authorized

    def set_source(self, name: str, *, measured_authorized: bool) -> None:
        """Record the detected source identity + authorization (lifespan loader).

        Set once at startup, not by the user toggle (mirrors
        ``cache_state.set_cache_available``).
        """
        with self._lock:
            self._source_name = str(name)
            self._measured_authorized = bool(measured_authorized)


# Shared, process-wide instance imported by main.py. Tests should construct
# their own ``PowerState()`` to stay isolated from this singleton.
state = PowerState()
