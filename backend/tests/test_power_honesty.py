# Feature: hardware-power-tracking, Property 1: a quality flag is never upgraded downstream
"""
Property 1 (Honesty) — MANDATORY.

*For any* ``PowerReading`` or set of readings tagged ``estimated`` or
``unavailable``, the quality flag carried through the REAL structures —
``PowerSampler.attribute_window`` (worst-quality-wins), the per-request
``power_report`` frame the way ``main.py`` builds it, and the ``PowerLog``
entry — is NEVER upgraded to ``measured``, and an ``unavailable`` reading is
NEVER relabeled ``estimated``. An estimate is never presented as a measurement.

Validates: Requirements 2.5, 2.6, 3.6, 3.7, 10.5.

This test operates on constructed ``PowerReading`` objects fed through the REAL
``attribute_window`` (readings are injected directly into a real sampler's
buffer) and the REAL ``PowerLog`` at a tmp path. It mocks nothing that would
need privilege: no sudo, no subprocess, no psutil.
"""

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from power_sampler import PowerSampler
from power_source import PowerReading
from power_log import PowerLog

QUALITIES = ("measured", "estimated", "unavailable")


class _FixedSource:
    """A source stub so PowerSampler has a name; its read() is never called
    here because we inject readings directly into the buffer."""

    name = "utilization-estimate"

    def read(self) -> PowerReading:  # pragma: no cover - not exercised
        return PowerReading(0.0, None, None, None, self.name, "unavailable")


def _reading(index: int, quality: str, watts: float, present: bool) -> PowerReading:
    """Build a reading at a deterministic timestamp inside a known window.

    ``present`` controls whether a numeric wattage is carried. A ``measured``
    reading with 0.0 W is intentionally allowed (to prove 0 stays measured-0,
    not unavailable).
    """
    # Timestamps 1.0, 2.0, ... all fall inside the window [0, 10000].
    ts = float(index + 1)
    if not present:
        return PowerReading(ts, None, None, None, "utilization-estimate", quality)
    return PowerReading(
        ts,
        cpu_watts=watts,
        gpu_watts=None,
        package_watts=watts,
        source="utilization-estimate",
        quality=quality,
    )


# A single reading: (quality, watts, present-numeric-flag).
_reading_spec = st.tuples(
    st.sampled_from(QUALITIES),
    st.floats(min_value=0.0, max_value=500.0, allow_nan=False, allow_infinity=False),
    st.booleans(),
)


def _build_report_frame(session_id, stage_enabled, attr):
    """Construct the per-request report dict the way main.py's frame builder
    will — inline here so this test does not depend on task-8 code."""
    return {
        "event": "power_report",
        "sessionId": session_id,
        "stageEnabled": stage_enabled,
        "quality": attr.quality,
        "source": attr.source,
        "avgPowerWatts": attr.avg_power_watts,
        "energyJoules": attr.energy_joules,
        "cpuWatts": attr.cpu_avg_watts,
        "gpuWatts": attr.gpu_avg_watts,
        "packageWatts": attr.package_avg_watts,
        "sampleCount": attr.sample_count,
        "durationSeconds": attr.duration_seconds,
    }


@settings(max_examples=200)
@given(specs=st.lists(_reading_spec, min_size=0, max_size=20), tmp=st.data())
def test_quality_flag_is_never_upgraded(specs, tmp, tmp_path_factory):
    # Build the readings and inject them into a REAL sampler buffer.
    readings = [
        _reading(i, quality, watts, present)
        for i, (quality, watts, present) in enumerate(specs)
    ]
    sampler = PowerSampler(_FixedSource(), interval_s=0.25, max_readings=3600)
    sampler._buf.extend(readings)

    # A window covering every constructed reading.
    attr = sampler.attribute_window(0.0, 10_000.0)

    # Classify what the window actually contains.
    usable = [
        r
        for r in readings
        if r.quality != "unavailable"
        and (r.package_watts is not None or r.cpu_watts is not None)
    ]
    has_usable = len(usable) > 0
    all_usable_measured = has_usable and all(r.quality == "measured" for r in usable)
    any_usable_estimated = any(r.quality == "estimated" for r in usable)

    # ---- HONESTY invariants on attribute_window ----

    # measured ONLY IF every usable reading is measured.
    if attr.quality == "measured":
        assert all_usable_measured, "measured requires ALL usable readings measured"

    # If any usable reading is estimated (and not all measured), never measured.
    if any_usable_estimated and not all_usable_measured:
        assert attr.quality != "measured", "estimate must never be upgraded to measured"

    # No usable numeric readings → unavailable with None figures (not 0).
    if not has_usable:
        assert attr.quality == "unavailable"
        assert attr.avg_power_watts is None
        assert attr.energy_joules is None
        assert attr.cpu_avg_watts is None
        assert attr.package_avg_watts is None

    # An entirely-unavailable set never yields estimated or measured.
    if readings and all(r.quality == "unavailable" for r in readings):
        assert attr.quality == "unavailable"

    # ---- Round-trip through the REAL frame builder ----
    frame = _build_report_frame("sess-1", True, attr)
    assert frame["quality"] == attr.quality  # frame never upgrades the flag
    if attr.quality == "unavailable":
        assert frame["avgPowerWatts"] is None
        assert frame["energyJoules"] is None

    # ---- Round-trip through the REAL PowerLog at a tmp path ----
    log_path = tmp_path_factory.mktemp("powerlog") / "power_log.jsonl"
    log = PowerLog(path=str(log_path))
    ok = log.append(
        "sess-1",
        avg_power_watts=attr.avg_power_watts,
        energy_joules=attr.energy_joules,
        source=attr.source,
        quality=attr.quality,
    )
    assert ok is True

    with open(log_path, "r", encoding="utf-8") as handle:
        logged = json.loads(handle.readlines()[-1])

    # The logged quality equals the attributed quality — never upgraded.
    assert logged["quality"] == attr.quality
    # Unavailable → null figures in the log, never 0.
    if attr.quality == "unavailable":
        assert logged["avg_power_watts"] is None
        assert logged["energy_joules"] is None


@settings(max_examples=100)
@given(watts=st.floats(min_value=0.0, max_value=500.0, allow_nan=False, allow_infinity=False))
def test_measured_zero_stays_measured_not_unavailable(watts, tmp_path_factory):
    """A measured reading (including 0.0 W) stays quality 'measured' with a
    numeric average, distinguishable from unavailable None."""
    reading = PowerReading(
        timestamp=5.0,
        cpu_watts=watts,
        gpu_watts=None,
        package_watts=watts,
        source="powermetrics",
        quality="measured",
    )
    sampler = PowerSampler(_FixedSource(), interval_s=0.25)
    sampler._buf.append(reading)

    attr = sampler.attribute_window(0.0, 10.0)

    assert attr.quality == "measured"
    assert attr.avg_power_watts == watts  # a genuine numeric value, incl. 0.0
    assert attr.avg_power_watts is not None

    # Log round-trip preserves the numeric 0.0 (never coerced to null).
    log_path = tmp_path_factory.mktemp("powerlog0") / "power_log.jsonl"
    log = PowerLog(path=str(log_path))
    log.append(
        "sess-0",
        avg_power_watts=attr.avg_power_watts,
        energy_joules=attr.energy_joules,
        source=attr.source,
        quality=attr.quality,
    )
    with open(log_path, "r", encoding="utf-8") as handle:
        logged = json.loads(handle.readlines()[-1])
    assert logged["quality"] == "measured"
    assert logged["avg_power_watts"] == watts
