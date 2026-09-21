# Requirements Document

## Introduction

TokenQuick adds a **hardware power / energy telemetry** stage to its FastAPI backend. A low-overhead background sampler periodically reads on-device hardware power (or, when true power is unavailable, a clearly-labeled utilization-based estimate), running off the request hot path. During the `/api/chat` "generate" phase — the PERFORM path (a single `invoke_sync` completion) and the BUILD path (`run_task_router`, multiple model calls) — the stage brackets the inference window and attributes a per-request average power (watts) and energy (joules) figure to that request. The result is streamed to the React dashboard as a new `power_report` data annotation, mirroring `compression_stats`, `redaction_report`, and `cache_report`, and rendered in a new right-pane "Power / Energy" panel.

The confirmed target machine is an Apple M5 (Mac17,3) running macOS (Darwin arm64), a unified-memory SoC where CPU and GPU share a single package power domain. The standard macOS tool `powermetrics` reports true package/CPU/GPU power but requires root/sudo, so privileged true-power access is **not** guaranteed to a background process. There is no NVIDIA GPU and no `nvidia-smi` on this machine, and `psutil` is not installed. The stage therefore uses a tiered sourcing model: true power when a privileged source is available (Apple Silicon `powermetrics`, or `nvidia-smi` on machines that have it), and graceful degradation to a labeled utilization-based estimate or an explicit "unavailable" reading otherwise — never crashing, never blocking the request, and never triggering an unprompted sudo/password prompt during a request.

Consistent with TokenQuick's "Zero Cloud Spend & Zero Local Waste — nothing leaves this machine" philosophy and its "never estimate when ground truth is available; be honest about it when you must" ethos, every power reading is tagged with its source and a `measured | estimated | unavailable` quality flag so the dashboard never presents an estimate as a measurement. All sampling and logging are on-device with no network egress. The stage is independently toggleable and independently benchmarkable, consistent with the compression, redaction, and semantic-cache stages.

## Glossary

- **Power_Stage**: The backend telemetry stage comprising the Power_Sampler, the Power_Source abstraction, the Per_Request_Power_Attribution, and the streamed Power_Report.
- **Power_Sampler**: The low-overhead background component that periodically reads hardware power or utilization at the Sampling_Interval, running off the request hot path, and starting/stopping with the application lifecycle.
- **Sampling_Interval**: The configurable time period between consecutive Power_Readings taken by the Power_Sampler.
- **Power_Source**: An abstraction over one concrete provider of a Power_Reading. Concrete sources are the Powermetrics_Source (Apple Silicon, privileged, true power), the Nvidia_Smi_Source (privileged/true GPU power where present), and the Utilization_Estimate_Source (labeled estimate).
- **Powermetrics_Source**: The Apple Silicon `powermetrics` provider that reports true Package_Power with CPU and GPU components; requires a privileged/pre-authorized path to run.
- **Nvidia_Smi_Source**: The `nvidia-smi` provider of true GPU power, available only on machines that have an NVIDIA GPU and the `nvidia-smi` tool.
- **Utilization_Estimate_Source**: A fallback provider that derives an estimated wattage from CPU utilization (e.g. via `psutil`) and a configurable TDP model, always labeled `estimated`.
- **Power_Reading**: A single timestamped observation containing available CPU, GPU, and Package_Power values (in watts), the identity of the producing Power_Source, and a Quality_Flag.
- **Quality_Flag**: The honesty tag on a Power_Reading or Power_Report; exactly one of `measured`, `estimated`, or `unavailable`.
- **Package_Power**: On a unified-memory SoC (Apple Silicon), the total package wattage of which CPU and GPU wattage are reported components. On a discrete-GPU machine, CPU and GPU power may come from separate rails.
- **Privileged_Source**: A Power_Source that can report true `measured` power (Powermetrics_Source or Nvidia_Smi_Source) and requires elevated privilege or a pre-authorized helper to run.
- **Inference_Window**: The time interval from the start to the end of the generate-phase model call(s) for one request (the `invoke_sync` call on the PERFORM path, or the full `run_task_router` span on the BUILD path).
- **Per_Request_Power_Attribution**: The computation that selects the Power_Readings falling within a request's Inference_Window and derives that request's Average_Power, Energy_Joules, sample count, and duration.
- **Average_Power**: The mean wattage across the Power_Readings attributed to a request's Inference_Window, in watts.
- **Energy_Joules**: The energy attributed to a request, computed as Average_Power (watts) multiplied by Inference_Window duration (seconds); equivalently watt-seconds.
- **Current_Power_Reading**: The most recent Power_Reading, exposed as an always-available live gauge value independent of any request.
- **Power_Report**: The per-request summary emitted to the dashboard via the `power_report` data annotation, carrying the per-request power/energy figures, the Power_Source identity, and the Quality_Flag.
- **Power_Benchmark**: The before/after presentation set for the Power_Stage (Average_Power, Energy_Joules, CPU/GPU/Package components, sample count, duration), consistent with the compression/redaction/cache benchmark pattern.
- **Power_Panel**: The frontend right-pane "Power / Energy" panel that renders the Power_Report.
- **Power_Log**: An append-only local log recording one entry per attributed request with power/energy figures and the Quality_Flag, and never any raw prompt content or sensitive value.
- **Backend**: The TokenQuick FastAPI application (`backend/main.py` and supporting modules).
- **Dashboard**: The TokenQuick React frontend right pane that renders live pipeline reports.
- **Session_Id**: The identifier for the current chat session used to scope the Power_Report and Power_Log entries.
- **Telemetry_Time_Budget**: The maximum time the Power_Stage waits for a Power_Reading before abandoning it as `unavailable` so telemetry never blocks a request; default 500 milliseconds.
- **Capability_Probe**: A non-interactive check run once at startup to determine whether a Privileged_Source is available and authorized without prompting for a password.

## Requirements

### Requirement 1: Low-overhead background power sampler

**User Story:** As a user, I want a lightweight background sampler that reads hardware power without slowing inference, so that I get power telemetry with no cost to responsiveness.

#### Acceptance Criteria

1. WHERE the Power_Stage is enabled, THE Power_Sampler SHALL run as a single shared background task (one instance per Backend process, not one per request) that takes a Power_Reading at each Sampling_Interval on a thread or async task separate from the request-handling path.
2. THE Power_Sampler SHALL read power or utilization at a configurable Sampling_Interval with a default value between 100 milliseconds and 1000 milliseconds inclusive.
3. THE Power_Sampler SHALL take every Power_Reading off the request hot path, such that the handling of any `/api/chat` generate request neither awaits, blocks on, nor acquires any lock or synchronization primitive shared with the Power_Sampler, and the request path does not wait for a Power_Reading to complete.
4. WHEN the Backend starts, THE Power_Sampler SHALL initialize and begin sampling as part of the application lifecycle startup.
5. WHEN the Backend shuts down, THE Power_Sampler SHALL stop sampling and release its sampling resources as part of the application lifecycle shutdown.
6. THE Power_Sampler SHALL read power and utilization using only on-device sources, and THE Backend SHALL make no external network call to take, store, or report any Power_Reading.
7. IF taking a single Power_Reading fails, THEN THE Power_Sampler SHALL record that Power_Reading with the `unavailable` Quality_Flag, continue sampling at the next Sampling_Interval, and leave request handling unaffected.
8. THE Power_Sampler SHALL retain Power_Readings in a fixed-capacity rolling buffer with a configurable maximum count (default 3600 readings), and WHEN the buffer is full, THE Power_Sampler SHALL discard the oldest Power_Reading before adding a new one so that retained-reading memory does not grow unbounded.

### Requirement 2: Power source abstraction and tiered sourcing

**User Story:** As a user, I want the system to use the best available power source and tell me which one it used, so that I can trust real measurements and recognize estimates.

#### Acceptance Criteria

1. THE Power_Stage SHALL expose a Power_Source abstraction under which the Powermetrics_Source, the Nvidia_Smi_Source, and the Utilization_Estimate_Source each provide a Power_Reading through the same interface.
2. WHEN the Backend starts, THE Power_Stage SHALL detect the available Power_Sources exactly once and fix the available set and the active Power_Source for the lifetime of the process.
3. WHEN selecting the active Power_Source, THE Power_Stage SHALL apply the precedence order Powermetrics_Source, then Nvidia_Smi_Source, then Utilization_Estimate_Source, selecting the first that is both available and authorized.
4. WHEN determining whether a Privileged_Source is available and authorized, THE Power_Stage SHALL run a non-interactive Capability_Probe that never prompts for a password or triggers a privilege-escalation dialog, and SHALL treat the source as not authorized if the Capability_Probe does not succeed within 5 seconds.
5. WHERE a Privileged_Source is available and authorized, THE Power_Stage SHALL select that Privileged_Source and tag its Power_Readings with the `measured` Quality_Flag.
6. WHERE no Privileged_Source is available or authorized and the Utilization_Estimate_Source is available, THE Power_Stage SHALL select the Utilization_Estimate_Source and tag its Power_Readings with the `estimated` Quality_Flag.
7. IF the Nvidia_Smi_Source tool is absent from the machine, THEN THE Power_Stage SHALL treat the Nvidia_Smi_Source as unavailable.
8. IF the utilization dependency required by the Utilization_Estimate_Source is absent, THEN THE Power_Stage SHALL treat the Utilization_Estimate_Source as unavailable and SHALL produce Power_Readings tagged `unavailable`.
9. WHERE no Privileged_Source is authorized and no Nvidia_Smi_Source is present, as on the confirmed Apple M5 target, THE Power_Stage SHALL select the Utilization_Estimate_Source and tag its readings `estimated` when the utilization dependency is present, or produce `unavailable` readings when it is absent.
10. THE Power_Stage SHALL tag every Power_Reading with the identity of the producing Power_Source and exactly one Quality_Flag of `measured`, `estimated`, or `unavailable`.
11. WHERE the active Power_Source is the Utilization_Estimate_Source, THE Utilization_Estimate_Source SHALL derive estimated wattage from CPU utilization and a configurable TDP model and SHALL tag the resulting Power_Reading `estimated`.
12. WHERE the active Power_Source reports Package_Power with CPU and GPU components, THE Power_Stage SHALL record the CPU component, the GPU component, and the Package_Power in the Power_Reading.

### Requirement 3: Graceful degradation without runtime privilege escalation

**User Story:** As a privacy- and stability-conscious user, I want power telemetry to degrade gracefully and never prompt me for a password mid-request, so that inference is never blocked or interrupted by the telemetry system.

#### Acceptance Criteria

1. IF no Privileged_Source is available or authorized, THEN THE Power_Stage SHALL continue operating using the Utilization_Estimate_Source or the `unavailable` Power_Reading and SHALL complete the generate request with the same success behavior it would have without telemetry.
2. WHEN handling a generate request, THE Power_Stage SHALL take every Power_Reading without spawning a privileged subprocess or any process that would trigger a privilege-escalation or password prompt, such that the request completes without any such prompt appearing.
3. IF true power requires a pre-authorized privileged helper or a one-time out-of-band setup step that has not been completed, THEN THE Power_Stage SHALL use the Utilization_Estimate_Source or the `unavailable` Power_Reading rather than triggering that setup during a request.
4. IF a Power_Source raises an error while producing a Power_Reading, THEN THE Power_Stage SHALL record the Power_Reading as `unavailable`, continue operating, and complete the generate request with the same success behavior and output it would have produced if the Power_Reading had succeeded.
5. IF a Power_Reading has not been produced within the Telemetry_Time_Budget of 500 milliseconds, THEN THE Power_Stage SHALL abandon that reading, record the Power_Reading as `unavailable`, and complete the generate request without waiting further for telemetry.
6. WHEN a Power_Reading is tagged `estimated`, THE Power_Stage SHALL carry the `estimated` Quality_Flag through the Per_Request_Power_Attribution, the Power_Report, and the Power_Log without relabeling it as `measured`.
7. THE Power_Stage SHALL never upgrade a Quality_Flag downstream, such that an `estimated` or `unavailable` Power_Reading is never relabeled as `measured` and an `unavailable` Power_Reading is never relabeled as `estimated` in the Per_Request_Power_Attribution, the Power_Report, or the Power_Log.
8. WHEN a Power_Reading is tagged `unavailable`, THE Power_Stage SHALL report the affected power figures as unavailable rather than reporting a fabricated numeric wattage.

### Requirement 4: Per-inference power and energy attribution

**User Story:** As a user, I want each inference's average power and energy attributed to that request, so that I can see the real hardware cost of a generation.

#### Acceptance Criteria

1. WHEN the generate phase begins the Inference_Window for a request, THE Per_Request_Power_Attribution SHALL record the window start time, and WHEN the generate phase ends the Inference_Window, THE Per_Request_Power_Attribution SHALL record the window end time.
2. WHEN a request's Inference_Window ends, THE Per_Request_Power_Attribution SHALL select the Power_Readings whose timestamps fall within the Inference_Window and compute the Average_Power in watts as the mean wattage across those Power_Readings.
3. WHEN a request's Inference_Window ends, THE Per_Request_Power_Attribution SHALL compute the Energy_Joules as the Average_Power in watts multiplied by the Inference_Window duration in seconds (joules = watts × seconds), reported as a non-negative value.
4. WHEN a request's Inference_Window ends, THE Per_Request_Power_Attribution SHALL report the count of Power_Readings attributed to the request as a non-negative integer and the Inference_Window duration in seconds as a non-negative value.
5. WHERE the active Power_Source reports CPU and GPU components, THE Per_Request_Power_Attribution SHALL report the attributed CPU average power and GPU average power in watts as components of the attributed Package_Power for the request.
6. WHEN the PERFORM path executes a single `invoke_sync` completion, THE Per_Request_Power_Attribution SHALL bracket the Inference_Window around that completion; and WHEN the BUILD path executes `run_task_router`, THE Per_Request_Power_Attribution SHALL bracket the Inference_Window around the full router span.
7. WHEN exactly one Power_Reading falls within a request's Inference_Window, THE Per_Request_Power_Attribution SHALL report the Average_Power as that single Power_Reading's wattage and a sample count of 1.
8. IF zero Power_Readings fall within a request's Inference_Window (for example an Inference_Window shorter than one Sampling_Interval), THEN THE Per_Request_Power_Attribution SHALL report the per-request power figures as `unavailable` with a sample count of 0 rather than reporting a fabricated Average_Power.
9. WHEN the Power_Readings attributed to a request carry mixed Quality_Flags, THE Per_Request_Power_Attribution SHALL resolve the per-request Quality_Flag by a worst-quality-wins rule: the result is `measured` only when every attributed reading is `measured`; the result is `estimated` when at least one attributed reading is `estimated` and none is `unavailable`-only preventing a numeric result; and the result is `unavailable` when no usable numeric reading is attributed.
10. THE Per_Request_Power_Attribution SHALL report `measured` only when the attributed Power_Readings are all `measured`, consistent with the worst-quality-wins rule.

### Requirement 5: Streaming the power report to the frontend

**User Story:** As a user, I want the per-request power figures streamed to the dashboard, so that I can see the power and energy of a generation as it completes.

#### Acceptance Criteria

1. WHEN the Per_Request_Power_Attribution completes for a generate request, THE Backend SHALL emit exactly one `power_report` data annotation tagged with the current Session_Id before the finish frame of the stream, on every generate path including a cache hit, a cache miss, the PERFORM path, and the BUILD path.
2. THE `power_report` annotation SHALL include the Average_Power in watts, the Energy_Joules in joules, the CPU and GPU component power in watts where the source provides them, the Package_Power in watts, the sample count as a non-negative integer, the Inference_Window duration in seconds, the Power_Source identity, and the Quality_Flag.
3. THE `power_report` annotation SHALL exclude any raw prompt content and any sensitive value.
4. WHERE a numeric power or energy field of the per-request result is unavailable, THE `power_report` annotation SHALL represent that field as null or omit it rather than reporting the value 0, so the Dashboard can distinguish a measured 0 watts from the absence of a reading.
5. WHEN the Quality_Flag of the per-request result is `estimated`, THE `power_report` annotation SHALL carry the `estimated` Quality_Flag together with the estimated numeric figures so the Dashboard can label the figures as estimated.
6. WHEN the Quality_Flag of the per-request result is `unavailable`, THE `power_report` annotation SHALL carry the `unavailable` Quality_Flag and SHALL represent the per-request power and energy figures as null or omitted rather than reporting a fabricated numeric wattage or energy value.
7. IF the Session_Id is absent from a generate request, THEN THE Backend SHALL reject the request before attributing power, consistent with the existing generate-request handling.

### Requirement 6: Power / Energy dashboard panel

**User Story:** As a user, I want a Power / Energy panel in the dashboard, so that I can see CPU, GPU, and package watts and energy per request with a clear source and quality indicator.

#### Acceptance Criteria

1. WHEN the Dashboard receives a `power_report` annotation whose Session_Id matches the current session, THE Dashboard SHALL render the Power_Panel in the right pane showing the CPU, GPU, and Package_Power in watts, the Energy_Joules for the request, and the Power_Source identity.
2. THE Power_Panel SHALL display a clear indicator distinguishing a `measured` result, an `estimated` result, and an `unavailable` result for the current reading.
3. WHILE the current session has received no `power_report` annotation, THE Power_Panel SHALL display an empty-state message indicating no power reading is available yet.
4. IF the Dashboard receives a `power_report` annotation whose Session_Id does not match the current session, THEN THE Dashboard SHALL leave the current Power_Panel contents unchanged.
5. WHEN the per-request Quality_Flag is `estimated`, THE Power_Panel SHALL label the displayed figures as estimated rather than as measured.
6. WHEN the per-request Quality_Flag is `unavailable`, THE Power_Panel SHALL indicate that power was unavailable for the request rather than displaying a fabricated wattage.

### Requirement 7: Toggleable power stage

**User Story:** As a user, I want to turn power telemetry on or off, so that I can control the stage consistent with the other pipeline stages.

#### Acceptance Criteria

1. WHEN a client requests the current Power_Stage toggle state through the Backend, THE Backend SHALL return whether the stage is enabled or disabled.
2. WHEN a client changes the Power_Stage toggle state through the Backend, THE Backend SHALL apply the new state to every generate request that begins after the change and SHALL leave any generate request already in progress running under the prior state.
3. WHERE the Power_Stage is disabled, THE Power_Sampler SHALL perform no sampling and THE Backend SHALL perform no Per_Request_Power_Attribution.
4. WHERE the Power_Stage is disabled, WHEN the generate phase completes, THE Backend SHALL emit a `power_report` annotation indicating that the stage was disabled for the request.
5. WHEN the Dashboard receives a `power_report` annotation indicating the stage was disabled, THE Power_Panel SHALL indicate that the Power_Stage is disabled.
6. WHEN the Backend starts with no prior toggle state configured, THE Backend SHALL default the Power_Stage to enabled.

### Requirement 8: Continuous background telemetry and live current-power reading

**User Story:** As a user, I want a continuously updated current-power reading in addition to per-request figures, so that I can watch a live gauge separate from any single generation.

#### Acceptance Criteria

1. WHERE the Power_Stage is enabled, THE Power_Sampler SHALL maintain a continuously updated Current_Power_Reading that reflects the most recent Power_Reading independent of any generate request.
2. WHEN a new Power_Reading is taken, THE Power_Stage SHALL update the Current_Power_Reading to that Power_Reading.
3. THE Current_Power_Reading SHALL carry the Power_Source identity and the Quality_Flag of the Power_Reading it reflects.
4. THE Per_Request_Power_Attribution SHALL derive its result solely from the Power_Readings within the Inference_Window and SHALL remain a distinct value from the Current_Power_Reading.
5. WHERE the Power_Stage is disabled, THE Power_Stage SHALL report the Current_Power_Reading as unavailable.

### Requirement 9: Power logging and benchmark consistency

**User Story:** As a user, I want an optional local power log and a before/after benchmark presentation, so that power telemetry matches the audit and benchmark patterns of the other stages.

#### Acceptance Criteria

1. WHERE Power_Log recording is enabled, WHEN the Per_Request_Power_Attribution completes for a request, THE Power_Log SHALL append one entry containing an ISO-8601 timestamp, the Session_Id, the Average_Power, the Energy_Joules, the Power_Source identity, and the Quality_Flag.
2. THE Power_Log SHALL record entries to a local append-only file and SHALL preserve all previously written entries unchanged when a new entry is appended.
3. THE Power_Log SHALL exclude any raw prompt content and any sensitive value from every entry.
4. IF appending an entry to the Power_Log fails, THEN THE Backend SHALL continue processing the request and record that the Power_Log entry could not be written.
5. WHEN the Per_Request_Power_Attribution completes, THE Power_Benchmark SHALL present the Average_Power, the Energy_Joules, the CPU, GPU, and Package_Power components, the sample count, and the Inference_Window duration for the request.
6. WHEN the Dashboard receives the Power_Benchmark figures, THE Dashboard SHALL display them in the right pane using the same layout, labeling, and units as the compression, redaction, and cache benchmark presentations.
7. IF the Power_Benchmark fails to capture any benchmark value, THEN THE Backend SHALL emit a data annotation indicating the power benchmark is unavailable and SHALL continue processing without blocking the generate output.

### Requirement 10: Safety and overhead constraints

**User Story:** As a user, I want strict guarantees that telemetry stays lightweight, on-device, and non-blocking, so that measuring power never harms inference, privacy, or stability.

#### Acceptance Criteria

1. THE Power_Sampler SHALL run as a non-blocking background task such that no Power_Reading and no Per_Request_Power_Attribution blocks, pauses, or measurably degrades the handling of a generate request.
2. THE Power_Stage SHALL require no elevated privilege at request time and SHALL take every request-time Power_Reading through a pre-authorized or unprivileged path.
3. IF any Power_Source, the Power_Sampler, or the Per_Request_Power_Attribution fails, THEN THE Power_Stage SHALL degrade to an `estimated` or `unavailable` result and SHALL leave the generate request completing normally.
4. THE Power_Stage SHALL make no external network call to sample, attribute, log, benchmark, or report power.
5. THE Power_Stage SHALL label every reported figure with its Quality_Flag such that an `estimated` figure is presented as estimated and a `measured` figure is presented as measured.

## Non-Goals and Constraints

- **Nothing leaves the machine.** THE Power_Sampler, THE Power_Source abstraction, THE Power_Log, and all power reporting SHALL operate entirely on-device, and THE Backend SHALL make no external network call to sample, attribute, log, benchmark, or report hardware power.
- **No unprompted privilege escalation at runtime.** The Power_Stage never silently invokes sudo or prompts for a password during a user request. If true power requires a pre-authorized privileged helper or a one-time setup step, that setup is out-of-band; absent it, the stage uses the labeled estimate or the unavailable path.
- **Honesty.** An estimate is always labeled `estimated`; a measurement is always labeled `measured`; an estimate is never presented as ground truth.
- **Apple Silicon unified-memory reality.** On the confirmed Apple M5 target, CPU and GPU power are reported as components of a shared Package_Power domain; the requirements do not assume independent discrete-GPU power rails on this machine, but the Power_Source abstraction allows an Nvidia_Smi_Source on machines that have an NVIDIA GPU.
- **Primary target is Apple Silicon `powermetrics`.** The `nvidia-smi` path is a secondary, optional source used only when present. `psutil` supports the utilization-based estimate only, never true wattage on macOS.
- **Single-machine, single-user scope.** No fleet, cluster, or cross-machine power aggregation.
