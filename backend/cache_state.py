"""
cache_state.py — In-memory toggle flag + availability gate + per-session cache stats

This module holds the runtime state that backs the hardened semantic-cache
stage:

  * The master ON/OFF toggle for the cache stage (``enabled``, default ``True``
    per Req 8.6).
  * The ``cache_available`` flag surfaced for the graceful-fallback path
    (Req 1.6/1.7/2.6). Startup wiring flips it to ``True`` once the embedding
    model AND the vector store both load; the stage runs only when the toggle
    is enabled AND the cache is available. Availability is set by the lifespan
    loader — it is NOT user-toggleable.
  * The per-session cumulative decision counts + savings that feed the
    dashboard's "Cache Report" panel via the ``cache_report`` data annotation
    (``session_id`` -> ``{hits, misses, tokensSaved, computeTimeSavedMs}``,
    Req 7.2/7.3).

The state is deliberately **in-memory only, lightweight, and JSON-serializable**.
It is never persisted to disk — session stats live only for the life of the
process (the *vector store* persists; this session telemetry does not,
matching ``redaction_state``). Snapshot accessors (``report_for``) return plain
``dict``/``int``/``float`` structures that ``json.dumps`` accepts directly, so
the payload can be streamed straight to the React frontend to populate the
Cache Report panel without any further conversion.

All shared-state reads and writes are guarded by a ``threading.Lock`` so
concurrent generate requests stay consistent, and every returned dict is a
fresh copy (a snapshot) — never a live reference to the internal state — so a
caller can ``json.dumps`` a result without racing an in-flight mutation.

Design reference: the "cache_state.py" section and the ``cache_report``
annotation contract of the hardened-semantic-cache design document.

Nothing in this module performs any network call.
"""

from __future__ import annotations

import threading

__all__ = ["CacheState", "state"]


class CacheState:
    """Runtime cache state: master toggle + availability gate + session stats.

    Instantiable so tests can create isolated instances; a shared module-level
    singleton (``state``) is also exposed for the FastAPI app, which imports one
    process-wide instance.

    Thread-safety: every read and write of ``enabled``, ``cache_available``, and
    ``_sessions`` happens under ``self._lock``. Accessor methods return copies
    (snapshots) so callers hold no live reference to the internal maps.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Master toggle — default enabled (Req 8.6).
        self._enabled: bool = True
        # Availability gate — set True once model + store load (Req 1.6/2.6).
        self._cache_available: bool = False
        # session_id -> {"hits", "misses", "tokensSaved", "computeTimeSavedMs"}.
        # In-memory only.
        self._sessions: dict[str, dict] = {}

    # -- Master toggle (Req 8.3, 8.4, 8.6) ---------------------------------

    def is_enabled(self) -> bool:
        """Return whether the cache stage toggle is currently on (Req 8.4)."""
        with self._lock:
            return self._enabled

    def set_enabled(self, value: bool) -> None:
        """Set the master toggle flag (Req 8.3 semantics — the flag only).

        Applying the flag at a request boundary is main.py's responsibility;
        this method just records the desired state.
        """
        with self._lock:
            self._enabled = bool(value)

    # -- Availability gate (Req 1.6, 1.7, 2.6) -----------------------------

    @property
    def cache_available(self) -> bool:
        """Return whether the model + vector store loaded and are usable.

        The stage runs only when the toggle is enabled AND this is True. This
        is set by the lifespan loader, not by the user toggle.
        """
        with self._lock:
            return self._cache_available

    def set_cache_available(self, value: bool) -> None:
        """Record whether the cache backends loaded successfully (Req 1.6/2.6)."""
        with self._lock:
            self._cache_available = bool(value)

    # -- Per-session accounting (Req 7.2, 7.3) -----------------------------

    def record_decision(
        self,
        session_id: str,
        *,
        hit: bool,
        tokens_saved: int = 0,
        compute_ms_saved: int = 0,
    ) -> None:
        """Record one cache decision for a session (Req 7.2/7.3).

        Increments ``hits`` or ``misses`` and adds to ``tokensSaved`` /
        ``computeTimeSavedMs``. Savings are clamped to be non-negative so the
        cumulative figures are monotonically non-decreasing (a negative input
        contributes 0). Thread-safe; creates the per-session entry on first use.
        """
        with self._lock:
            session = self._sessions.setdefault(
                session_id,
                {"hits": 0, "misses": 0, "tokensSaved": 0, "computeTimeSavedMs": 0},
            )
            if hit:
                session["hits"] += 1
            else:
                session["misses"] += 1
            session["tokensSaved"] += max(0, int(tokens_saved))
            session["computeTimeSavedMs"] += max(0, int(compute_ms_saved))

    def report_for(self, session_id: str) -> dict:
        """Return a JSON-serializable ``cache_report`` snapshot for a session.

        Shape mirrors the design's ``cache_report`` accounting payload::

            {"hits", "misses", "decisions", "hitRate",
             "tokensSavedFromCache", "computeTimeSavedMs"}

        where ``decisions = hits + misses`` and ``hitRate = hits / decisions``
        in ``0.0..1.0`` (``0.0`` when ``decisions == 0``). An unknown / empty
        session yields the zero state. The returned dict is a fresh copy, safe
        to ``json.dumps``.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                hits = misses = tokens_saved = compute_ms = 0
            else:
                hits = session["hits"]
                misses = session["misses"]
                tokens_saved = session["tokensSaved"]
                compute_ms = session["computeTimeSavedMs"]

        decisions = hits + misses
        hit_rate = (hits / decisions) if decisions > 0 else 0.0
        return {
            "hits": hits,
            "misses": misses,
            "decisions": decisions,
            "hitRate": hit_rate,
            "tokensSavedFromCache": tokens_saved,
            "computeTimeSavedMs": compute_ms,
        }

    def reset_session(self, session_id: str) -> None:
        """Drop the in-memory stats for a session (test/utility helper).

        Session stats are in-memory only; this simply forgets a session's
        accumulated counts. It does not touch the persistent vector store or
        the append-only cache-decision log.
        """
        with self._lock:
            self._sessions.pop(session_id, None)


# Shared, process-wide instance imported by main.py. Tests should construct
# their own ``CacheState()`` to stay isolated from this singleton.
state = CacheState()
