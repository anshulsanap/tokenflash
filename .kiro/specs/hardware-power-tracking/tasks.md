# Implementation Plan: Hardware Power / Energy Tracking

## Overview

This plan converts the hardware-power-tracking design into a series of incremental,
bottom-up coding steps, mirroring the Phase 1/2 module patterns (a state singleton
like `cache_state.py`/`redaction_state.py`, an append-only log like `cache_log.py`,
pure helpers wired — not reimplemented — in `main.py`, and presentational React
panels wired through `page.tsx`). Each step builds on the previous one and ends by
wiring new code into the running system, so no code is left orphaned.

The stage adds four backend modules (`power_source.py`, `power_sampler.py`,
`power_state.py`, `power_log.py`), extends `backend/main.py` (lifespan, generate
handler, `/api/power/*` endpoints), and adds the `PowerReport.tsx` panel plus
`page.tsx` / `RedactionSettings.tsx` wiring on the frontend.

**Test policy (per explicit user directive):** ONLY the test covering the
structural Honesty invariant — design **Property 1** (a quality flag is never
upgraded downstream; an estimate is never presented as measured) — is MANDATORY and
appears as a normal required checkbox. EVERY other test (Properties 2–13, all unit
tests, all FastAPI TestClient integration tests, all frontend component tests) is
marked OPTIONAL/deferrable with the `- [ ]*` starred convention.

## Tasks

- [x] 1. Add the `psutil` runtime dependency for the estimated tier
  - Edit `backend/requirements.txt`: add `psutil==6.1.0` with the estimated-tier
    comment block explaining that psutil reads CPU utilization locally (no network),
    powers the ESTIMATED tier only (never true wattage on macOS), and the MEASURED
    tier comes from the system `powermetrics` binary (no Python dependency). Pin to
    the Python 3.13-compatible (cp313) release, matching the existing pinning
    discipline.
  - _Requirements: 2.6, 2.8_

- [ ] 2. Implement the power source abstraction and startup detection (`backend/power_source.py`)
  - [x] 2.1 Define `PowerReading`, the `Quality` type, and the `PowerSource` protocol
    - Create `backend/power_source.py` with `from __future__ import annotations`.
    - Define `Quality = Literal["measured", "estimated", "unavailable"]`.
    - Define the frozen `@dataclass(frozen=True) PowerReading` with fields
      `timestamp: float`, `cpu_watts: float | None`, `gpu_watts: float | None`,
      `package_watts: float | None`, `source: str`, `quality: Quality`. Document that
      `None` (never `0`) means "component not reported / unavailable" so a measured
      `0.0 W` stays distinguishable.
    - Define the `PowerSource` Protocol: `name: str` and `read() -> PowerReading`
      (contract: never raises, returns an `unavailable` reading on any failure, no
      network call).
    - _Requirements: 2.1, 2.10, 2.12, 3.8_

  - [x] 2.2 Implement `UtilizationEstimateSource` (estimated tier, default on M5)
    - `name = "utilization-estimate"`; `__init__(self, tdp_watts=20.0, idle_watts=2.0)`
      for the configurable TDP model.
    - `read()`: lazy-import `psutil`; compute estimated watts from
      `psutil.cpu_percent()` × the TDP model and return a reading tagged `estimated`
      (package/cpu populated, gpu `None` on unified-memory). If `psutil` is not
      importable OR the read fails, return an `unavailable` reading (never raise).
    - _Requirements: 2.6, 2.8, 2.11, 3.4_

  - [x] 2.3 Implement `NvidiaSmiSource` (discrete-GPU measured tier)
    - `name = "nvidia-smi"`; `read()` runs
      `nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits` via
      subprocess (lazy import) and returns a `measured` GPU reading on clean parse.
    - If the binary is absent (`FileNotFoundError`) or any failure occurs, return an
      `unavailable` reading; the source is treated as unavailable in precedence.
    - _Requirements: 2.7, 3.4_

  - [x] 2.4 Implement `PowermetricsSource` and its parser (Apple Silicon measured tier)
    - `name = "powermetrics"`; `read()` runs
      `sudo -n powermetrics --samplers cpu_power -n 1 -i <ms>` with the
      Telemetry_Time_Budget timeout. The `-n` flag guarantees no password prompt: on
      any failure (`TimeoutExpired`, non-zero exit, missing binary) return an
      `unavailable` reading (never raise, never prompt).
    - Implement a pure `parse_powermetrics_output(text) -> PowerReading`-style helper
      that extracts CPU / GPU / package power lines → a `measured` reading with all
      three components; malformed/partial output yields an `unavailable` reading
      rather than a fabricated number.
    - _Requirements: 2.5, 2.12, 3.2, 3.4_

  - [x] 2.5 Implement `probe_powermetrics_authorized` gated by `POWER_TRY_MEASURED`
    - `probe_powermetrics_authorized(timeout_s: float = 5.0) -> bool`.
    - Return `False` IMMEDIATELY without invoking `sudo` unless
      `os.environ.get("POWER_TRY_MEASURED") == "1"` — so by DEFAULT the backend never
      runs `sudo` at all (no prompt, no logged failed-sudo attempt).
    - When the flag is `"1"`, run `sudo -n powermetrics --samplers cpu_power -n 1 -i 200`
      once with the 5s timeout; return `True` ONLY on a clean exit within budget.
      `TimeoutExpired`, non-zero exit, or missing binary all return `False`
      (fail-closed). Never raise.
    - _Requirements: 2.4, 3.2, 3.3_

  - [x] 2.6 Implement `detect_power_source` precedence selection
    - `detect_power_source(*, tdp_watts=20.0) -> tuple[PowerSource, bool]`.
    - Apply precedence `powermetrics → nvidia-smi → utilization-estimate`, selecting
      the first source that is BOTH available AND authorized; use
      `probe_powermetrics_authorized()` (the gated probe) for the powermetrics tier
      and a non-privileged availability check for nvidia-smi. Return
      `(active_source, measured_authorized)`. When nothing usable is available, return
      a source whose readings are all `unavailable`.
    - Runs exactly once (called from lifespan); no probe runs during a request.
    - _Requirements: 1.6, 2.2, 2.3, 2.9_

  - [ ]* 2.7 Write property test for source precedence selection
    - **Property 10: Source precedence selection**
    - **Validates: Requirements 2.3, 2.5, 2.6, 2.9**
    - OPTIONAL/deferrable. Hypothesis, ≥100 iterations; mock probe/availability
      flags; tag `# Feature: hardware-power-tracking, Property 10: Source precedence selection`.

  - [ ]* 2.8 Write property test for the powermetrics parser round-trip
    - **Property 11: Powermetrics parse round-trip**
    - **Validates: Requirements 2.12**
    - OPTIONAL/deferrable. Hypothesis, ≥100 iterations; synthesize powermetrics output
      and mock subprocess; malformed input → `unavailable`.

  - [ ]* 2.9 Write unit tests for source `read()` unavailable-on-failure behavior
    - OPTIONAL/deferrable. Mock `psutil`/subprocess to raise and to be absent; assert
      each source returns an `unavailable` reading and never raises; assert the gated
      probe never invokes `sudo` when `POWER_TRY_MEASURED` is unset/0.
    - _Requirements: 2.7, 2.8, 3.2, 3.4_

- [ ] 3. Implement the background sampler and attribution (`backend/power_sampler.py`)
  - [x] 3.1 Define `PowerAttribution` and the `PowerSampler` skeleton
    - Create `backend/power_sampler.py`. Define the `PowerAttribution` result type with
      `avg_power_watts`, `energy_joules`, `cpu_avg_watts`, `gpu_avg_watts`,
      `package_avg_watts` (all `float | None`), `sample_count: int`,
      `duration_seconds: float`, `quality: Quality`, `source: str`.
    - Define `PowerSampler.__init__(source, *, interval_s=0.25, max_readings=3600,
      time_budget_s=0.5)` holding a `collections.deque(maxlen=max_readings)`, a
      `threading.Lock`, `_current`, `_thread`, and a `threading.Event` stop flag.
      Validate/clamp `interval_s` into `[0.1, 1.0]`.
    - _Requirements: 1.1, 1.2, 1.8_

  - [x] 3.2 Implement the sampler loop, `start`/`stop`, and `current_reading`
    - `start()` spawns exactly ONE daemon thread; `stop()` signals the event and joins.
    - `_run()` loops every `interval_s`: read the source guarded by the
      Telemetry_Time_Budget; a read that raises or exceeds the budget yields an
      `unavailable` `PowerReading` and the loop continues. Append under the lock and
      update `_current` (the continuous Current_Power_Reading).
    - `current_reading` property returns `_current` under the lock (frozen dataclass,
      safe to hand out).
    - _Requirements: 1.3, 1.4, 1.5, 1.7, 3.5, 8.1, 8.2, 8.3, 10.1_

  - [x] 3.3 Implement `_snapshot` and `attribute_window`
    - `_snapshot()` copies `tuple(self._buf)` under the lock and returns it; all math
      runs OUTSIDE the lock (the request path never holds the lock across a read).
    - `attribute_window(start_ts, end_ts) -> PowerAttribution`: select readings with
      `start_ts <= r.timestamp <= end_ts`; compute `avg_power_watts` = mean of usable
      wattages, `energy_joules` = `avg × max(0, end-end)` (non-negative),
      cpu/gpu/package averages over readings that carry each component, `sample_count`,
      `duration_seconds = max(0, end_ts - start_ts)`.
    - Resolve `quality` by worst-quality-wins: `measured` only if every attributed
      reading is `measured`; `estimated` if ≥1 usable reading is `estimated` and none
      forces unavailability; `unavailable` when no usable numeric reading is attributed.
      Zero readings → `unavailable`, `sample_count 0`, all numeric figures `None`;
      the n=1 case reports that single reading's wattage with `sample_count 1`.
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 4.7, 4.8, 4.9, 4.10, 8.4, 10.1_

  - [ ]* 3.4 Write property test for energy and averaging math
    - **Property 2: Energy and averaging math**
    - **Validates: Requirements 4.2, 4.3, 4.5, 4.7, 2.11**
    - OPTIONAL/deferrable. Hypothesis, ≥100 iterations; tag
      `# Feature: hardware-power-tracking, Property 2: Energy and averaging math`.

  - [ ]* 3.5 Write property test for worst-quality-wins resolution
    - **Property 3: Worst-quality-wins resolution**
    - **Validates: Requirements 4.9, 4.10**
    - OPTIONAL/deferrable. Hypothesis, ≥100 iterations; mixed-quality generators; tag
      `# Feature: hardware-power-tracking, Property 3: Worst-quality-wins resolution`.

  - [ ]* 3.6 Write property test for window selection and empty-window unavailability
    - **Property 4: Window selection and empty-window unavailability**
    - **Validates: Requirements 4.2, 4.4, 4.8**
    - OPTIONAL/deferrable. Hypothesis, ≥100 iterations; windows straddling/containing/
      excluding readings, including empty and n=1.

  - [ ]* 3.7 Write property test for the bounded, non-blocking, on-device sampler
    - **Property 7: Bounded, non-blocking, on-device sampler**
    - **Validates: Requirements 1.3, 1.6, 1.8, 10.1, 10.4**
    - OPTIONAL/deferrable. Hypothesis, ≥100 iterations; assert buffer length =
      `min(N, max_readings)`, snapshot-under-lock, and no network egress.

  - [ ]* 3.8 Write property test for the continuous current reading
    - **Property 13: Continuous current reading equals the most recent sample**
    - **Validates: Requirements 8.1, 8.2, 8.3**
    - OPTIONAL/deferrable. Hypothesis, ≥100 iterations.

  - [ ]* 3.9 Write property test for degradation without runtime privilege escalation
    - **Property 8: Degradation without runtime privilege escalation**
    - **Validates: Requirements 1.7, 3.1, 3.2, 3.4, 10.2, 10.3**
    - OPTIONAL/deferrable. Hypothesis, ≥100 iterations; failing reads/probe → recorded
      `unavailable`, no prompting subprocess spawned.

- [x] 4. Implement the power state singleton (`backend/power_state.py`, mirrors `cache_state.py`)
  - Create `backend/power_state.py` with a `PowerState` class: thread-safe
    (`threading.Lock`), master toggle default enabled (Req 7.6); `is_enabled()` /
    `set_enabled(value)`; `source_name` and `measured_authorized` read-only
    properties set once by `set_source(name, *, measured_authorized)` from the
    lifespan loader (not user-toggleable, mirroring `cache_available` vs `enabled`).
  - Expose a module-level singleton `state = PowerState()` imported by `main.py`;
    keep it in-memory only and JSON-serializable.
  - _Requirements: 7.1, 7.2, 7.6_

- [x] 5. Implement the append-only power log (`backend/power_log.py`, mirrors `cache_log.py`)
  - Create `backend/power_log.py` with a `PowerLog` class: `DEFAULT_POWER_LOG_PATH`
    at `backend/power_log.jsonl`; minimal public surface (constructor, read-only
    `path`, `append(...)`) with NO clear/delete/truncate/rotate method; file only
    ever opened in append mode `'a'`.
  - `append(session_id, *, avg_power_watts, energy_joules, source, quality) -> bool`:
    serialize one JSON object per line OUTSIDE a `threading.Lock`, write+flush under
    the lock; write `avg_power_watts`/`energy_joules` as JSON `null` (never `0`) when
    the per-request result is `unavailable`. Entries carry ONLY scalars — no raw
    prompt content or sensitive value.
  - Catch all errors, log a WARNING, return `False` (never crash the caller).
  - _Requirements: 9.1, 9.2, 9.3, 9.4_

- [x] 6. Write the MANDATORY Honesty property test (Property 1) — REQUIRED, not deferrable
  - **MANDATORY — must not be deferred.** This is the single required test task; it
    guards the load-bearing structural invariant of the whole stage.
  - **Property 1: Honesty — a quality flag is never upgraded downstream**
  - **Validates: Requirements 2.5, 2.6, 3.6, 3.7, 10.5**
  - Create `backend/test_power_honesty.py` (or the project's test location). Use
    Hypothesis with a minimum of 100 iterations. Tag the test with the exact comment:
    `# Feature: hardware-power-tracking, Property 1: a quality flag is never upgraded downstream`.
  - Generators produce `PowerReading`s / sets of readings tagged `estimated` or
    `unavailable` (including mixed-quality sets, all-unavailable sets, and measured-0-W
    readings). Assert that the quality carried through the REAL structures —
    `PowerSampler.attribute_window` (worst-quality-wins), the `main.py` `power_report`
    frame builder, and the `PowerLog` entry — is NEVER upgraded to `measured`, and an
    `unavailable` reading is NEVER relabeled `estimated`. An estimate is never
    presented as a measurement.
  - Exercise the real attribution, frame builder, and log entry construction (not
    mocks of them); mock ONLY `psutil`/subprocess for source reads so the test needs
    no privilege and never spawns `sudo`.
  - _Requirements: 2.5, 2.6, 3.6, 3.7, 10.5_

- [x] 7. Checkpoint - backend core complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Wire the stage into `backend/main.py` (lifespan, attribution, `power_report`)
  - [x] 8.1 Extend `lifespan` to detect the source and manage the sampler
    - In the existing `asynccontextmanager` lifespan (do not add a second one): call
      `detect_power_source(...)` once (the gated probe — never invokes `sudo` unless
      `POWER_TRY_MEASURED=1`), call `power_state.set_source(name,
      measured_authorized=...)`, construct the module-global `power_sampler =
      PowerSampler(active_source, ...)` and call `power_sampler.start()` ONLY if
      `power_state.is_enabled()`, and construct `power_log = PowerLog()`.
    - Add module globals `power_sampler: PowerSampler | None = None` and
      `power_log: PowerLog | None = None` alongside the redaction/cache singletons.
    - After `yield` (shutdown), call `power_sampler.stop()` to join the daemon thread.
    - _Requirements: 1.4, 1.5, 2.2, 7.3_

  - [x] 8.2 Add the `power_report` / `power_benchmark_unavailable` frame builders
    - Add pure helpers mirroring `_cache_report_frame` /
      `_cache_benchmark_unavailable_frame`: `_power_report_frame(session_id,
      stage_enabled, attr)` returning a `data_annotation` with `event: "power_report"`,
      `sessionId`, `stageEnabled`, `quality`, `source`, `avgPowerWatts`, `energyJoules`,
      `cpuWatts`, `gpuWatts`, `packageWatts`, `sampleCount`, `durationSeconds`.
      Unavailable/disabled numeric fields are JSON `null` (never `0`); no raw content.
      Include measured / estimated / unavailable / disabled variants.
    - Add `_power_benchmark_unavailable_frame(session_id)` fallback
      (`event: "power_benchmark_unavailable"`).
    - _Requirements: 5.2, 5.4, 5.5, 5.6, 7.4, 9.5, 9.7_

  - [x] 8.3 Snapshot the toggle and add the `_attribute_power` helper
    - At generate-request entry, snapshot `gen_power_enabled = power_state.is_enabled()`
      alongside the existing `gen_cache_enabled` snapshot (in-flight requests keep the
      snapshot). The existing missing-`sessionId` → HTTP 400 guard already precedes any
      attribution (Req 5.7).
    - Add `_attribute_power(enabled, start, end) -> PowerAttribution`: returns a
      disabled marker attribution when `not enabled`; an `unavailable` attribution when
      the sampler is `None` or the window has zero readings; otherwise
      `power_sampler.attribute_window(start, end)`.
    - _Requirements: 4.8, 5.7, 7.2, 7.3, 10.3_

  - [x] 8.4 Bracket the inference window on the PERFORM and BUILD paths
    - PERFORM (miss branch): set `window_start = time.time()` immediately before
      `invoke_sync(...)` and `window_end = time.time()` immediately after it returns;
      then `power_attr = _attribute_power(gen_power_enabled, window_start, window_end)`.
    - BUILD (miss branch): set `window_start`/`window_end` around `run_task_router(...)`
      the same way; then attribute.
    - _Requirements: 4.1, 4.6_

  - [x] 8.5 Emit exactly one `power_report` before finish on every path (incl. cache hit)
    - On the cache-HIT path, construct the honest `unavailable` attribution directly
      (`sample_count 0`, figures `None`, `quality "unavailable"`, `source =` active
      source name) — no inference was performed.
    - On EVERY path (hit, miss, PERFORM, BUILD, disabled) emit exactly one
      `_power_report_frame(...)` immediately BEFORE `finish_message`, wrapped in
      try/except that falls back to `_power_benchmark_unavailable_frame(...)` and
      continues (telemetry must not block output). Then best-effort
      `power_log.append(...)` when enabled and `power_log is not None`.
    - _Requirements: 4.8, 5.1, 7.4, 8.4, 9.1, 9.7_

  - [ ]* 8.6 Write property test for null-not-zero unavailable figures
    - **Property 6: Unavailable figures are null, never a fabricated zero**
    - **Validates: Requirements 3.8, 4.8, 5.4, 5.6**
    - OPTIONAL/deferrable. Hypothesis, ≥100 iterations against the real frame builder
      and log entry.

  - [ ]* 8.7 Write property test for no raw value in any report or log entry
    - **Property 5: No raw value in any power report or log entry**
    - **Validates: Requirements 5.3, 9.3**
    - OPTIONAL/deferrable. Hypothesis, ≥100 iterations; adversarial prompt substrings
      (length ≥ 4) must not appear in any `power_report` payload or `power_log` entry.

  - [ ]* 8.8 Write property test for the disabled-stage behavior
    - **Property 9: Disabled stage takes no readings and reports disabled**
    - **Validates: Requirements 7.3, 8.5**
    - OPTIONAL/deferrable. Hypothesis, ≥100 iterations.

  - [ ]* 8.9 Write property test for append-only log integrity
    - **Property 12: Append-only log integrity**
    - **Validates: Requirements 9.2**
    - OPTIONAL/deferrable. Hypothesis, ≥100 iterations; prior lines preserved
      byte-for-byte, file only grows, no clear/truncate/rotate on the surface.

  - [ ]* 8.10 Write FastAPI TestClient integration tests for the generate paths
    - OPTIONAL/deferrable. Drive `/api/chat` generate for a cache HIT, a miss→PERFORM,
      and a miss→BUILD; assert exactly one `power_report` appears and precedes the `d:`
      finish frame; disabled toggle → `stageEnabled: false`, `quality: "unavailable"`,
      null figures; empty/unavailable window → numeric fields JSON `null` not `0`;
      mock the source so the test needs no privilege.
    - _Requirements: 5.1, 5.4, 5.6, 7.4_

- [x] 9. Checkpoint - backend wiring complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Add the `/api/power/*` FastAPI endpoints
  - [x] 10.1 Add the toggle endpoints
    - `GET /api/power/toggle` returns `{"enabled": power_state.is_enabled()}` (Req 7.1).
    - `POST /api/power/toggle` (reuse the existing `ToggleBody`): call
      `power_state.set_enabled(body.enabled)`; when `power_sampler is not None`, start
      it (idempotent) on enable or stop it on disable, so a disabled stage performs no
      sampling; return the new state. New state applies to requests that BEGIN after
      the change; in-flight requests keep their snapshot.
    - _Requirements: 7.1, 7.2, 7.3_

  - [x] 10.2 Add the read-only current-reading endpoint
    - `GET /api/power/current`: when disabled or `power_sampler is None`, return the
      `unavailable` shape (`quality: "unavailable"`, source name, null cpu/gpu/package);
      otherwise return the current reading's quality, source, and cpu/gpu/package watts
      (null-not-zero preserved). Only a lock-guarded snapshot read; never blocks the
      sampler.
    - _Requirements: 8.1, 8.2, 8.3, 8.5_

  - [ ]* 10.3 Write TestClient tests for the endpoints
    - OPTIONAL/deferrable. Toggle round-trip and default-enabled; `/api/power/current`
      shape and `unavailable` when disabled; `PowerLog` pointed at an unwritable path
      → `append` returns `False` and the request still completes.
    - _Requirements: 7.1, 7.2, 7.6, 8.5, 9.4_

- [x] 11. Frontend: the Power / Energy panel and wiring
  - [x] 11.1 Create `frontend/components/PowerReport.tsx`
    - Pure presentational panel mirroring `CacheReport.tsx` with an amber/yellow
      accent. Export the `PowerReportData` interface (`stageEnabled`, `quality`,
      `source`, `avgPowerWatts`, `energyJoules`, `cpuWatts`, `gpuWatts`,
      `packageWatts`, `sampleCount`, `durationSeconds`) and
      `PowerReport({ report }: { report: PowerReportData | null })`.
    - Render: empty-state ("No power reading yet for this session."); a quality badge
      for `measured` (amber solid) / `estimated` (amber outline + "estimated") /
      `unavailable` (slate) / `stage off` when `stageEnabled` is false; CPU/GPU/Package
      watts and Energy (J) via the shared `StatBadge`; a `null` field renders as "—"
      (never `0`); the source identity. No fetching, no hooks.
    - _Requirements: 6.1, 6.2, 6.3, 6.5, 6.6, 7.5, 9.6_

  - [x] 11.2 Wire `power_report` handling into `frontend/app/page.tsx`
    - Add `const [powerReport, setPowerReport] = useState<PowerReportData | null>(null)`.
    - In `processLine`, add a session-guarded handler for `event === "power_report"`
      (`payload.sessionId === sessionId`) that sets state using `?? null` (NOT `?? 0`)
      to preserve null-not-zero; add a `power_benchmark_unavailable` handler that clears
      `powerReport`.
    - Reset `setPowerReport(null)` in both `triggerGenerate` and `resetChat`.
    - Render `<PowerReport report={powerReport} />` in the right pane after
      `<CacheReport ... />`.
    - _Requirements: 6.1, 6.4, 6.6_

  - [x] 11.3 Add the Power Telemetry toggle row to `RedactionSettings.tsx`
    - Add a third toggle row "Power Telemetry: On/Off" beside the Redaction and
      Semantic Cache switches, reusing the switch markup and the `handleToggle`
      pattern. Add `powerEnabled` / `powerTogglePending` / `powerToggleError` state,
      load it in the initial `Promise.all` fetch (adding
      `fetch(\`${BACKEND}/api/power/toggle\`)`), and flip it via a `handlePowerToggle`
      mirroring `handleCacheToggle`, hitting `/api/power/toggle`.
    - _Requirements: 7.1, 7.2_

  - [ ]* 11.4 Write frontend component tests for `PowerReport.tsx`
    - OPTIONAL/deferrable. Render each variant (measured / estimated / unavailable /
      disabled) and the empty state; assert the correct badge; assert a
      session-mismatched annotation leaves contents unchanged.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 7.5_

- [x] 12. Document the optional measured tier in the README
  - Edit `README.md` to document the one-time, out-of-band operator opt-in that unlocks
    the `measured` tier: grant passwordless `sudo` for `powermetrics` via an
    `/etc/sudoers.d/tokenquick-powermetrics` snippet scoped to `/usr/bin/powermetrics`
    and a single user, and note that the backend only DETECTS this via the gated
    `POWER_TRY_MEASURED=1` probe — it never writes sudoers, never prompts, and defaults
    to the `estimated` tier. Include the security caveat.
  - _Requirements: 3.3, 10.2_

- [x] 13. Final checkpoint - ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- **Test policy (per explicit user directive): only the Property 1 (Honesty) test is
  mandatory; all other tests are optional/deferrable.** Task 6 (Property 1 — a quality
  flag is never upgraded downstream) is a normal REQUIRED checkbox and must not be
  deferred. Every other test — the remaining correctness Properties 2–13, all unit
  tests, all FastAPI TestClient integration tests, and all frontend component tests —
  is marked with the `- [ ]*` starred convention and may be skipped for a faster MVP.
- Tasks marked with `*` are optional and can be skipped without blocking the feature.
- The `psutil==6.1.0` dependency (Task 1) powers the ESTIMATED tier; the MEASURED tier
  uses the system `powermetrics` binary (no Python dependency).
- The `powermetrics` capability probe is GATED behind `POWER_TRY_MEASURED`: by default
  the backend never invokes `sudo` and selects the estimated tier directly (Task 2.5).
- Each task references specific requirements for traceability; the mandatory test also
  references its property number.
- Checkpoints (Tasks 7, 9, 13) provide incremental validation at natural breaks.
- Property tests use Hypothesis with ≥100 iterations and mock `psutil`/subprocess so
  they need no privilege and never spawn `sudo`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1", "2.1", "4", "5"] },
    { "id": 1, "tasks": ["2.2", "2.3", "2.4", "3.1"] },
    { "id": 2, "tasks": ["2.5", "3.2"] },
    { "id": 3, "tasks": ["2.6", "3.3", "2.9", "12"] },
    { "id": 4, "tasks": ["2.7", "2.8", "3.4", "3.5", "3.6", "3.7", "3.8", "3.9"] },
    { "id": 5, "tasks": ["8.1"] },
    { "id": 6, "tasks": ["8.2", "8.3"] },
    { "id": 7, "tasks": ["8.4", "8.5"] },
    { "id": 8, "tasks": ["6", "8.6", "8.7", "8.8", "8.9", "10.1", "10.2"] },
    { "id": 9, "tasks": ["8.10", "10.3", "11.1"] },
    { "id": 10, "tasks": ["11.2", "11.3"] },
    { "id": 11, "tasks": ["11.4"] }
  ]
}
```
