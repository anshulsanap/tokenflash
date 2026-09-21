"""
power_sampler.py — Background power sampler + per-request attribution

This module implements the runtime half of the hardware-power-tracking stage:

  * ``PowerAttribution`` — the immutable result of bracketing one Inference_Window
    (Req 4.2–4.10): average power, energy, per-component averages, sample count,
    duration, quality flag, and the active source name.
  * ``PowerSampler``     — the single shared background daemon thread (Req 1.1–1.8,
    8.1–8.3, 10.1). It reads the source every Sampling_Interval into a
    fixed-capacity rolling buffer under a lock, maintains the continuous
    Current_Power_Reading, and computes per-request attribution from a snapshot
    copied under the lock (the request path never holds the lock across a model
    call).
  * ``disabled_attribution`` / ``unavailable_attribution`` — honest all-``None``
    markers used by the disabled and cache-hit paths in ``main.py``.

The load-bearing honesty invariant (Req 3.6, 3.7, 4.9, 4.10): the per-request
quality flag is resolved by WORST-QUALITY-WINS and is never upgraded — an
estimate is never presented as a measurement.

Design references: the "Components and Interfaces → power_sampler.py",
"Per-request attribution", and Correctness Properties 1–4 sections of the
hardware-power-tracking design document. Nothing here makes a network call.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass

from power_source import PowerReading, PowerSource, Quality

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PowerAttribution (Req 4.2–4.10)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PowerAttribution:
    """The per-request result of bracketing one Inference_Window.

    Numeric fields are watts / joules or ``None``. ``None`` (never ``0``) means
    unavailable (Req 3.8, 4.8). ``quality`` is resolved by worst-quality-wins
    over the attributed readings and is never upgraded downstream (Req 4.9, 4.10).
    """

    avg_power_watts: float | None
    energy_joules: float | None
    cpu_avg_watts: float | None
    gpu_avg_watts: float | None
    package_avg_watts: float | None
    sample_count: int
    duration_seconds: float
    quality: Quality
    source: str


def disabled_attribution(source_name: str) -> PowerAttribution:
    """Honest marker for the DISABLED path — no sampling, no math (Req 7.3, 7.4)."""
    return PowerAttribution(
        avg_power_watts=None,
        energy_joules=None,
        cpu_avg_watts=None,
        gpu_avg_watts=None,
        package_avg_watts=None,
        sample_count=0,
        duration_seconds=0.0,
        quality="unavailable",
        source=source_name,
    )


def unavailable_attribution(
    source_name: str, duration: float = 0.0
) -> PowerAttribution:
    """Honest marker for an empty window / cache hit (Req 4.8, cache-hit design)."""
    return PowerAttribution(
        avg_power_watts=None,
        energy_joules=None,
        cpu_avg_watts=None,
        gpu_avg_watts=None,
        package_avg_watts=None,
        sample_count=0,
        duration_seconds=max(0.0, duration),
        quality="unavailable",
        source=source_name,
    )


def _mean(values: list[float]) -> float | None:
    """Arithmetic mean of a non-empty list, or ``None`` when empty."""
    return sum(values) / len(values) if values else None


# ---------------------------------------------------------------------------
# PowerSampler (Req 1.1–1.8, 8.1–8.3, 10.1)
# ---------------------------------------------------------------------------

class PowerSampler:
    """One shared background sampler for the whole process.

    A single daemon thread calls ``source.read()`` every ``interval_s`` and
    appends the ``PowerReading`` to a ``deque(maxlen=max_readings)`` under a lock,
    updating the Current_Power_Reading. The request path NEVER holds this lock
    across a model call or an await: ``attribute_window`` copies a snapshot under
    the lock and computes OUTSIDE it (Req 1.3, 10.1).
    """

    def __init__(
        self,
        source: PowerSource,
        *,
        interval_s: float = 0.25,
        max_readings: int = 3600,
        time_budget_s: float = 0.5,
    ):
        self._source = source
        # Clamp the interval into [0.1, 1.0] seconds (Req 1.2).
        self._interval = min(1.0, max(0.1, interval_s))
        self._time_budget = time_budget_s
        self._buf: collections.deque[PowerReading] = collections.deque(
            maxlen=max_readings
        )
        self._lock = threading.Lock()
        self._current: PowerReading | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- Lifecycle (Req 1.4, 1.5) ------------------------------------------

    def start(self) -> None:
        """Spawn exactly ONE daemon thread; idempotent (no-op if running)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the sampler to stop and join the daemon thread (bounded)."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            # Bounded join so shutdown never hangs on a slow source read.
            thread.join(timeout=self._interval + self._time_budget + 1.0)
        self._thread = None

    def _run(self) -> None:
        """Sampler loop: read, append under lock, wait (responsive to stop).

        The source already never raises, but be defensive: a read that raises
        synthesizes an ``unavailable`` reading and the loop continues (Req 1.7,
        3.4). ``stop_event.wait(interval)`` provides the sleep so ``stop()`` is
        responsive.
        """
        while not self._stop.is_set():
            try:
                reading = self._source.read()
            except Exception as err:  # noqa: BLE001 — defensive; source shouldn't raise
                logger.warning("Sampler read raised; recording unavailable: %s", err)
                reading = PowerReading(
                    timestamp=time.time(),
                    cpu_watts=None,
                    gpu_watts=None,
                    package_watts=None,
                    source=getattr(self._source, "name", "unknown"),
                    quality="unavailable",
                )
            with self._lock:
                self._buf.append(reading)
                self._current = reading
            self._stop.wait(self._interval)

    # -- Current reading (Req 8.1–8.3) -------------------------------------

    @property
    def current_reading(self) -> PowerReading | None:
        """The most recent reading (frozen dataclass — safe to hand out)."""
        with self._lock:
            return self._current

    # -- Snapshot + attribution (Req 4.2–4.10) -----------------------------

    def _snapshot(self) -> tuple[PowerReading, ...]:
        """Copy the buffer under the lock; all math runs OUTSIDE the lock."""
        with self._lock:
            return tuple(self._buf)

    def attribute_window(self, start_ts: float, end_ts: float) -> PowerAttribution:
        """Bracket one Inference_Window and compute the per-request figures.

        Selects readings with ``start_ts <= r.timestamp <= end_ts`` (inclusive),
        then:

          * ``avg_power_watts`` = mean of usable readings' package wattage
            (falling back to cpu wattage when package is absent);
          * ``energy_joules`` = ``avg_power_watts × duration`` (non-negative);
          * ``cpu/gpu/package`` averages = mean over readings carrying each
            component;
          * ``sample_count`` = number of readings selected in the window;
          * ``duration_seconds`` = ``max(0, end_ts - start_ts)``;
          * ``quality`` by WORST-QUALITY-WINS (Req 4.9, 4.10):
              - ``measured`` only if EVERY usable reading is ``measured``;
              - ``estimated`` if >=1 usable reading is ``estimated`` and there is
                >=1 usable reading;
              - ``unavailable`` if there are NO usable numeric readings.

        Zero selected → ``unavailable``, ``sample_count`` 0, all ``None``. A single
        usable reading → its own wattage. A quality flag is NEVER upgraded.
        """
        snapshot = self._snapshot()
        source_name = getattr(self._source, "name", "unknown")
        duration = max(0.0, end_ts - start_ts)

        selected = [r for r in snapshot if start_ts <= r.timestamp <= end_ts]
        sample_count = len(selected)

        # A reading is "usable" when it carries a numeric wattage and is not
        # explicitly unavailable.
        def _usable_watt(reading: PowerReading) -> float | None:
            if reading.quality == "unavailable":
                return None
            if reading.package_watts is not None:
                return reading.package_watts
            if reading.cpu_watts is not None:
                return reading.cpu_watts
            return None

        usable = [r for r in selected if _usable_watt(r) is not None]

        if not usable:
            # No usable numeric reading → unavailable (may still have selected>0).
            return PowerAttribution(
                avg_power_watts=None,
                energy_joules=None,
                cpu_avg_watts=None,
                gpu_avg_watts=None,
                package_avg_watts=None,
                sample_count=sample_count,
                duration_seconds=duration,
                quality="unavailable",
                source=source_name,
            )

        watts = [w for w in (_usable_watt(r) for r in usable) if w is not None]
        avg_power = _mean(watts)
        energy = None
        if avg_power is not None:
            energy = max(0.0, avg_power * duration)

        cpu_avg = _mean([r.cpu_watts for r in usable if r.cpu_watts is not None])
        gpu_avg = _mean([r.gpu_watts for r in usable if r.gpu_watts is not None])
        package_avg = _mean(
            [r.package_watts for r in usable if r.package_watts is not None]
        )

        # Worst-quality-wins over the USABLE readings (never upgrade).
        if all(r.quality == "measured" for r in usable):
            quality: Quality = "measured"
        else:
            # There is >=1 usable reading and not all are measured → estimated.
            quality = "estimated"

        return PowerAttribution(
            avg_power_watts=avg_power,
            energy_joules=energy,
            cpu_avg_watts=cpu_avg,
            gpu_avg_watts=gpu_avg,
            package_avg_watts=package_avg,
            sample_count=sample_count,
            duration_seconds=duration,
            quality=quality,
            source=source_name,
        )
