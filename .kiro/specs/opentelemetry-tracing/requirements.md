# Requirements Document

## Introduction

This feature adds **OpenTelemetry (OTEL) distributed tracing** to TokenQuick's
FastAPI backend. Today the backend records rich operational telemetry to three
append-only local JSONL logs (`audit_log.py` for redaction, `cache_log.py` for
cache decisions, `power_log.py` for power/energy attribution) plus in-memory
state singletons. This feature wraps those same operations as standard OTEL
**trace spans** — a parent span per `/api/chat` generate request with child
spans for each pipeline stage — so the pitch moves from "I logged data to a
file" to "I emit standard distributed-tracing spans that a collector such as
Jaeger or Prometheus can ingest."

The work is **additive and identity-preserving**. It must not weaken any of the
guarantees the existing stages established:

- **Local-first, $0, deny-outbound by default.** Tracing must work with **zero
  outbound network calls** out of the box (a console/file span exporter is the
  default). Exporting to an external OTLP collector is an explicit, documented,
  opt-in — the single sanctioned egress — gated behind an environment variable,
  mirroring the `POWER_TRY_MEASURED` gate pattern.
- **Honesty invariants.** Span attributes carry ONLY the same scalars and
  metadata the JSONL logs already carry — never a raw prompt, redacted-prompt
  text, or any sensitive value; and the null-vs-zero distinction is preserved
  (an unavailable figure is never emitted as a fabricated `0`).
- **Never block or crash the request.** Instrumentation failures degrade
  silently (best-effort), exactly like the append-only logs do today.
- **The JSONL logs remain.** OTEL spans are a parallel, additive dual-write; the
  existing append-only logs are NOT removed or replaced.

## Glossary

- **Span** — a single OTEL unit of work with a name, start/end time, status, and
  attributes. Spans nest to form a trace.
- **Request_Span** — the parent span covering one `/api/chat` generate request.
- **Stage_Span** — a child span for a pipeline stage (redaction, cache lookup,
  compression, inference, power attribution).
- **Span_Attribute** — a scalar key/value on a span (e.g. `cache.decision="hit"`,
  `power.quality="estimated"`). Attributes are the span analogue of a JSONL log
  field and are subject to the SAME zero-raw-value rule.
- **Tracer_Provider** — the OTEL object that creates tracers and owns the
  configured Span_Exporter(s); constructed once at startup.
- **Span_Exporter** — the sink spans are sent to. The **Console/File_Exporter**
  is the zero-network default; the **OTLP_Exporter** sends to an external
  collector and is opt-in only.
- **OTEL_Gate** — the environment-variable switch that enables the OTLP_Exporter.
  When unset/off, no OTLP endpoint is contacted and no outbound call is made.
- **Honesty_Invariant** — the rule that a Span_Attribute contains only the
  scalars/metadata the corresponding JSONL log contains: no raw value, and
  `null`/absent rather than a fabricated `0` for an unavailable figure.
- **Sanctioned_Egress** — the ONE outbound network path this feature may add
  (OTLP export to an operator-configured collector), active only when the
  OTEL_Gate is on.

---

## Requirements

### Requirement 1: OTEL tracing scaffold and tracer provider

**User Story:** As an engineer, I want a single OTEL tracer provider initialized
at startup, so that the whole backend emits spans through one consistent
pipeline.

#### Acceptance Criteria

1.1. WHEN the FastAPI app starts, THEN the system SHALL initialize exactly one
Tracer_Provider once (in the existing lifespan handler), configured with a
resource identifying the service (e.g. `service.name = "tokenquick-backend"`).

1.2. THE Tracer_Provider SHALL be constructed with the Console/File_Exporter as
the default sink so tracing functions with zero outbound network calls.

1.3. THE OTEL dependencies SHALL be added to `backend/requirements.txt` with
pinned versions compatible with the existing Python 3.13 environment.

1.4. IF OTEL initialization fails for any reason, THEN the system SHALL log a
WARNING and continue serving requests normally with tracing effectively
disabled — startup SHALL NOT crash because of tracing.

1.5. THE tracing scaffold SHALL be isolated in its own module (e.g.
`backend/tracing.py`) exposing a small surface (initialize, get tracer, shutdown)
so `main.py` wires it rather than embedding provider construction inline.

### Requirement 2: Zero-network default; opt-in OTLP export (Sanctioned_Egress)

**User Story:** As a privacy- and cost-conscious user, I want tracing to make no
outbound calls unless I explicitly opt in, so that TokenQuick's "$0, local,
deny-outbound" identity is preserved by default.

#### Acceptance Criteria

2.1. WHEN no OTEL_Gate environment variable is set (the default), THEN the system
SHALL use ONLY the Console/File_Exporter and SHALL make NO outbound network
connection for tracing.

2.2. WHEN the OTEL_Gate is set to enable OTLP export (e.g.
`OTEL_EXPORTER_OTLP_ENDPOINT` is present, or a `TOKENQUICK_OTEL=1` flag plus an
endpoint), THEN the system SHALL additionally configure an OTLP_Exporter pointing
at the operator-provided collector endpoint.

2.3. THE OTLP_Exporter SHALL be the ONLY outbound network path this feature adds,
and it SHALL be active ONLY when the OTEL_Gate is on (Sanctioned_Egress).

2.4. WHEN the OTEL_Gate is off, THEN the code path that constructs the
OTLP_Exporter SHALL NOT execute and no OTLP endpoint SHALL be contacted (fail
closed to local-only), mirroring the `POWER_TRY_MEASURED` gate.

2.5. IF the OTLP_Exporter cannot reach its configured collector at runtime, THEN
the failure SHALL be handled by the OTEL SDK's own best-effort export (spans may
be dropped) and SHALL NOT block, delay, or crash the request flow.

### Requirement 3: Request span for the generate pipeline

**User Story:** As an engineer, I want one parent span per generate request, so
that a trace shows the full request and its stages nested underneath.

#### Acceptance Criteria

3.1. WHEN a `/api/chat` request runs the generate phase, THEN the system SHALL
create one Request_Span covering the request.

3.2. THE Request_Span SHALL carry only scalar attributes: at minimum the
`session_id`, the phase (`generate`), and the resolved task mode
(`build`/`perform`) — and SHALL NOT carry the raw prompt, requirements summary,
redacted text, compressed text, or generated code.

3.3. THE Request_Span SHALL record a span status of error (and an exception event
WITHOUT raw content) IF the request raises, so failures are visible in the trace.

3.4. Stage_Spans (Requirement 4) SHALL be created as children of the
Request_Span so the trace reflects the pipeline nesting.

### Requirement 4: Stage spans mirroring the existing pipeline and logs

**User Story:** As an engineer, I want each pipeline stage traced as a child
span whose attributes mirror what the stage already logs, so the trace and the
JSONL logs tell the same story.

#### Acceptance Criteria

4.1. THE system SHALL create a redaction Stage_Span whose attributes mirror the
redaction telemetry (e.g. per-category counts, total redactions, chars redacted,
latency) — carrying the SAME metadata already recorded, never a raw or
redacted value.

4.2. THE system SHALL create a cache-lookup Stage_Span whose attributes mirror
`cache_log.py`: `decision` (hit/miss), `top_score`, `runner_up_score`, `margin`,
and `had_runner_up` — scores and scalars only, no raw value.

4.3. THE system SHALL create a compression Stage_Span whose attributes carry only
scalar metrics (e.g. original/compressed token counts, ratio) and never the raw
or compressed prompt text.

4.4. THE system SHALL create an inference Stage_Span around the model call
(PERFORM `invoke_sync` and BUILD `run_task_router`) carrying only scalar metadata
(e.g. real input/output token counts, inference time) and never generated code
or prompt text.

4.5. THE system SHALL create a power-attribution Stage_Span whose attributes
mirror `power_log.py`: `avg_power_watts`, `energy_joules`, `source`, and
`quality` — preserving null-not-zero (an unavailable figure is absent or null,
never a fabricated `0`).

4.6. WHERE a stage is disabled or produces an unavailable result, THE
corresponding Stage_Span SHALL reflect that honestly (e.g. a `stage.enabled=false`
attribute or an `unavailable` quality) rather than emitting fabricated figures.

### Requirement 5: Honesty invariant on span attributes (zero raw value, null-not-zero)

**User Story:** As a security-conscious maintainer, I want span attributes held
to the same zero-raw-value standard as the JSONL logs, so that adding tracing
never becomes a new leak channel.

#### Acceptance Criteria

5.1. NO Span_Attribute or span event on ANY span SHALL contain a raw prompt, raw
requirements summary, redacted-prompt text, compressed-prompt text, generated
code, or any sensitive value — only the scalars/metadata the corresponding JSONL
log already stores.

5.2. THE null-vs-zero distinction SHALL be preserved on span attributes: an
unavailable numeric figure (e.g. power when `quality="unavailable"`) SHALL be
represented as absent/null on the span, NEVER as a fabricated `0`.

5.3. THE set of attributes emitted per stage SHALL be a subset of (or equal to)
the fields the corresponding JSONL log records for that stage — tracing SHALL NOT
introduce a new field that carries more sensitive information than the log.

5.4. THE Honesty_Invariant SHALL be enforced by an automated property test that,
given adversarial raw inputs, asserts no ≥4-character substring of a raw/sensitive
value appears in any emitted span's attributes or events, and that unavailable
figures never surface as `0`.

### Requirement 6: Never block or crash the request (best-effort)

**User Story:** As a user, I want tracing to be invisible when it fails, so a
tracing problem never degrades or breaks a generation.

#### Acceptance Criteria

6.1. WHEN span creation, attribute setting, or export fails for any reason, THEN
the failure SHALL be caught/handled and the request SHALL continue and complete
normally (best-effort, mirroring the append-only logs' `return False` behavior).

6.2. THE tracing code SHALL NOT add a blocking network call on the request's
critical path; OTLP export SHALL be handled by the SDK's batching/background
export so request latency is not gated on a collector round-trip.

6.3. WHEN the app shuts down, THEN the Tracer_Provider SHALL be flushed/shut down
cleanly (best-effort) so buffered spans are exported without hanging shutdown.

### Requirement 7: Coexistence with the existing JSONL logs (additive dual-write)

**User Story:** As a maintainer, I want the existing append-only logs kept
intact, so tracing is additive and nothing that already works is disturbed.

#### Acceptance Criteria

7.1. THE existing `audit_log.py`, `cache_log.py`, and `power_log.py` append-only
JSONL writers SHALL remain in place and continue to be written exactly as they
are today; this feature SHALL NOT remove, replace, or alter their public surface
or their append-only / zero-raw-value guarantees.

7.2. THE OTEL spans SHALL be emitted in ADDITION to the JSONL log writes
(dual-write), sourced from the same in-memory values, so the trace and the log
agree.

7.3. THE behavior of the redaction, cache, power, and artifact stages (including
their toggles and their streamed data annotations to the frontend) SHALL be
unchanged by adding tracing.

### Requirement 8: Documentation of the opt-in collector path

**User Story:** As an operator, I want clear docs on how to turn on OTLP export
to a collector, so I can view traces in Jaeger/Prometheus when I choose to.

#### Acceptance Criteria

8.1. THE README (or a dedicated doc) SHALL document that tracing defaults to the
zero-network Console/File_Exporter, and that OTLP export to an external collector
is an explicit opt-in via the OTEL_Gate env var(s), naming the Sanctioned_Egress.

8.2. THE docs SHALL include a minimal example of enabling OTLP export (the env
var(s) to set and a pointer to running a local collector such as Jaeger), and the
security note that this is the one outbound path tracing adds and is off by
default.

---

## Non-functional & scope notes

- **Testing posture:** the load-bearing automated test is the Honesty_Invariant
  property test (Requirement 5.4). Beyond that, lightweight unit/integration
  tests confirm spans are created for each stage with the expected scalar
  attributes and that the OTEL_Gate defaults to local-only (no OTLP construction
  when the gate is off). Heavy property-based testing beyond the honesty check is
  not required.
- **Scope:** backend-only. No frontend changes. No change to the streamed data
  annotations, toggles, or any stage's behavior. Python 3.13 env; pin OTEL deps.
- **Non-goals:** metrics/OTEL-metrics pipelines, log-to-OTEL bridging, and a
  bundled collector are out of scope; this feature is trace spans + an opt-in
  OTLP exporter only.

## Open questions for the design phase

1. **(Req 2)** Exact OTEL_Gate semantics: rely solely on the standard
   `OTEL_EXPORTER_OTLP_ENDPOINT` presence, or add an explicit `TOKENQUICK_OTEL=1`
   flag in addition, to make the opt-in unambiguous and consistent with the
   `POWER_TRY_MEASURED` precedent.
2. **(Req 1.2)** Console vs file for the default exporter (and if file, where it
   lives and whether it is append-only like the JSONL logs).
3. **(Req 4)** Whether to reuse the official FastAPI OTEL auto-instrumentation for
   the HTTP layer or hand-instrument only the generate pipeline to keep the
   dependency surface minimal.
4. **(Req 5.4)** Where the honesty property test hooks in (an in-memory span
   exporter capturing emitted spans) to assert no raw substring leaks.
