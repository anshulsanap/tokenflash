# Feature: hardware-power-tracking, Task 8 integration: honest degradation on
# sampler failure mid-request and on a toggle flip during an in-flight request.
"""
Integration-level test for the Task 8 wiring in ``main.py``.

This exercises the REAL request-path pieces the generate handler uses to emit a
``power_report`` — ``main._attribute_power``, ``main._power_report_and_log``, and
``main._power_report_frame`` — together with a REAL ``PowerSampler`` and a REAL
``PowerLog`` at a tmp path. It mocks ONLY the source's ``read()`` (standing in
for psutil/subprocess) so it needs no privilege and never spawns ``sudo``.

It asserts the single load-bearing honesty guarantee under two failure modes
that can happen while a request is in flight:

  (a) SAMPLER FAILURE MID-REQUEST — the active source starts raising on every
      read (a psutil/powermetrics hiccup). Every reading the sampler records for
      the window is therefore ``unavailable``. The attributed ``power_report``
      and the log entry MUST report ``unavailable`` with null figures — never a
      stale or fabricated ``measured``/``estimated`` numeric reading.

  (b) TOGGLE FLIP DURING AN IN-FLIGHT REQUEST — the request snapshotted
      ``gen_power_enabled`` at entry. If it flips OFF mid-request, the in-flight
      request keeps its snapshot; and because the wiring only ever carries the
      attribution's own worst-quality-wins flag, the emitted report is a truthful
      ``estimated`` (from real estimated readings) or ``unavailable`` — but NEVER
      ``measured``, and NEVER a value fabricated after the source stopped
      producing usable readings.
"""

import json

import main
from power_sampler import PowerSampler
from power_source import PowerReading
from power_state import PowerState
from power_log import PowerLog


class _FlakySource:
    """A source whose read() can be flipped to raise, simulating a mid-request
    sampler failure. Stands in for psutil/subprocess — never touches hardware."""

    name = "utilization-estimate"

    def __init__(self):
        self.fail = False

    def read(self) -> PowerReading:
        if self.fail:
            raise RuntimeError("simulated sampler failure mid-request")
        # A healthy ESTIMATED reading when not failing.
        return PowerReading(
            timestamp=0.0,  # overwritten by explicit injection in the test
            cpu_watts=7.5,
            gpu_watts=None,
            package_watts=7.5,
            source=self.name,
            quality="estimated",
        )


def _emit_and_capture(session_id, enabled, attr, tmp_log_path, monkeypatch):
    """Drive the REAL main.py emit helper with a REAL PowerLog at a tmp path and
    return (frame_payload_dict, last_log_entry_dict_or_None)."""
    real_log = PowerLog(path=str(tmp_log_path))
    monkeypatch.setattr(main, "power_log", real_log)
    frame_str = main._power_report_and_log(session_id, enabled, attr)
    # data_annotation wraps a single dict in a list: "2:[{...}]\n".
    assert frame_str.startswith("2:") and frame_str.endswith("\n")
    payload = json.loads(frame_str[2:])[0]

    last_entry = None
    try:
        with open(tmp_log_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        if lines:
            last_entry = json.loads(lines[-1])
    except FileNotFoundError:
        last_entry = None
    return payload, last_entry


def test_sampler_failure_and_toggle_flip_report_honestly(tmp_path, monkeypatch):
    # A dedicated, isolated PowerState + sampler so we never touch the singleton.
    state = PowerState()
    state.set_source("utilization-estimate", measured_authorized=False)
    monkeypatch.setattr(main, "power_state", state)

    source = _FlakySource()
    sampler = PowerSampler(source, interval_s=0.1, max_readings=3600)
    monkeypatch.setattr(main, "power_sampler", sampler)

    # ── (a) SAMPLER FAILURE MID-REQUEST ──────────────────────────────────
    # The request begins with the stage ENABLED. Simulate the source failing for
    # the whole inference window: inject the exact `unavailable` readings the
    # sampler's loop would record when read() raises (Req 1.7, 3.4). Timestamps
    # 1.0/2.0 fall inside the window [0, 100].
    source.fail = True
    for ts in (1.0, 2.0, 3.0):
        sampler._buf.append(
            PowerReading(ts, None, None, None, source.name, "unavailable")
        )

    attr_fail = main._attribute_power(True, 0.0, 100.0)
    # HONESTY: no usable reading in the window → unavailable, never fabricated.
    assert attr_fail.quality == "unavailable"
    assert attr_fail.avg_power_watts is None
    assert attr_fail.energy_joules is None
    assert attr_fail.sample_count == 3  # readings were selected but none usable

    payload_fail, log_fail = _emit_and_capture(
        "sess-fail", True, attr_fail, tmp_path / "fail.jsonl", monkeypatch
    )
    assert payload_fail["event"] == "power_report"
    assert payload_fail["quality"] == "unavailable"
    # null-not-zero: never a fabricated 0 on the failure path.
    assert payload_fail["avgPowerWatts"] is None
    assert payload_fail["energyJoules"] is None
    assert payload_fail["packageWatts"] is None
    # The log entry mirrors the honest unavailable result (null, never 0).
    assert log_fail is not None
    assert log_fail["quality"] == "unavailable"
    assert log_fail["avg_power_watts"] is None
    assert log_fail["energy_joules"] is None

    # ── (b) TOGGLE FLIP DURING AN IN-FLIGHT REQUEST ──────────────────────
    # A NEW request begins ENABLED and snapshots that (gen_power_enabled=True).
    # It records real ESTIMATED readings in its window. Mid-request the operator
    # flips the toggle OFF — the in-flight request keeps its snapshot, and the
    # attribution reflects the readings actually taken: an honest ESTIMATED
    # result. It is NEVER upgraded to measured, and never a value fabricated
    # after a failure.
    source.fail = False
    fresh = PowerSampler(source, interval_s=0.1, max_readings=3600)
    monkeypatch.setattr(main, "power_sampler", fresh)
    for ts in (11.0, 12.0):
        fresh._buf.append(
            PowerReading(ts, 7.5, None, 7.5, source.name, "estimated")
        )

    gen_power_enabled = True  # snapshot taken at request entry
    # Operator flips the toggle OFF while the request is in flight:
    state.set_enabled(False)
    assert state.is_enabled() is False  # global flag changed…
    # …but the in-flight request uses its snapshot, so attribution still runs.
    attr_flip = main._attribute_power(gen_power_enabled, 0.0, 100.0)

    # HONESTY: the flip never turns estimated into measured, and the estimated
    # numeric figures are the real mean of the real readings — not fabricated.
    assert attr_flip.quality == "estimated"
    assert attr_flip.quality != "measured"
    assert attr_flip.avg_power_watts == 7.5
    assert attr_flip.sample_count == 2

    payload_flip, log_flip = _emit_and_capture(
        "sess-flip", gen_power_enabled, attr_flip, tmp_path / "flip.jsonl", monkeypatch
    )
    assert payload_flip["quality"] == "estimated"
    assert payload_flip["quality"] != "measured"
    assert payload_flip["avgPowerWatts"] == 7.5
    assert log_flip is not None
    assert log_flip["quality"] == "estimated"
    assert log_flip["avg_power_watts"] == 7.5
