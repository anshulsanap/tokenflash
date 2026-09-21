"""
artifact_state.py — In-memory toggle for the on-device artifacts stage

This module holds the runtime state that backs the private-on-device-artifacts
(Sandpack live-preview) stage, mirroring ``power_state.py`` / ``cache_state.py``:

  * The master ON/OFF toggle for the artifact stage (``enabled``, default
    ``True`` per design §1, resolving open question Q6 — consistent with the
    other stages' default-on posture).

Unlike ``power_state.py``, this stage has NO detected-source identity or
measured-authorization concept: it is purely a prompt-shaping + UI flag. The
only runtime state is the on/off toggle.

The state is deliberately **in-memory only, lightweight, and JSON-serializable**
(never persisted to disk). All shared-state reads and writes are guarded by a
``threading.Lock`` so concurrent generate requests stay consistent.

Design reference: the "Components and Interfaces → 1. Backend: prompt injection
+ toggle" section of the private-on-device-artifacts design document. Nothing
here makes a network call.
"""

from __future__ import annotations

import threading

__all__ = ["ArtifactState", "state"]


class ArtifactState:
    """Runtime artifact-stage state: the master on/off toggle.

    Instantiable so tests can create isolated instances; a shared module-level
    singleton (``state``) is also exposed for the FastAPI app.

    Thread-safety: every read and write happens under ``self._lock``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Master toggle — default enabled (design §1, Q6).
        self._enabled: bool = True

    # -- Master toggle (Req 8.1, 8.4) --------------------------------------

    def is_enabled(self) -> bool:
        """Return whether the artifact stage toggle is currently on (Req 8.1)."""
        with self._lock:
            return self._enabled

    def set_enabled(self, value: bool) -> None:
        """Set the master toggle flag (Req 8.3 — the flag only).

        Applying the flag at a request boundary is main.py's responsibility;
        this method just records the desired state.
        """
        with self._lock:
            self._enabled = bool(value)


# Shared, process-wide instance imported by main.py. Tests should construct
# their own ``ArtifactState()`` to stay isolated from this singleton.
state = ArtifactState()
