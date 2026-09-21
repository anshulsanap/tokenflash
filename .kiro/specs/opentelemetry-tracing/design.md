# Design Document

## Overview

This feature adds **OpenTelemetry (OTEL) trace spans** to the FastAPI backend as
an additive, identity-preserving layer over the existing telemetry. A single
Tracer_Provider is initialized once at startup; the `/api/chat` generate request
is hand-instrumented with one parent Request_Span and one child Stage_Span per
pipeline stage (redaction, cache lookup, compression, inference, power
attribution). Spans carry ONLY the same scalars the existing JSONL logs already
record, and the whole thing degrades silently on any failure.

Four decisions are locked (from the requirements open-questions), and the design
below implements them exactly:

1. **Gate semantics** — `TOKENQUICK_OTEL_EGRESS` (default `false`). When
   `false`, export is LOCAL-ONLY. When `true`, an OTLP exporter is added pointing
   at the standard `OTEL_EXPORTER_OTLP_ENDPOINT` — the single Sanctioned_Egress.
2. **Default exporter** — a custom **append-only JSONL file exporter** writing to
   `backend/logs/traces.jsonl` (NOT console), so the terminal stays clean.
3. **Instrumentation strategy** — **hand-instrumentation only**. No FastAPI
   auto-instrumentation, so no unvetted HTTP headers or request bodies can leak
   into span attributes. We explicitly wrap `/api/chat` and each stage.
4. **Honesty test** — `backend/tests/test_tracing_honesty.py` (pytest): no ≥4-char
   substring of a raw prompt/secret appears in any exported span attribute, and
   unavailable power figures export as `None`/null, never `0`.

This design is grounded in the real OTEL Python SDK: the `SpanExporter`
interface (`export(spans)` + `shutdown()`), `BatchSpanProcessor` for
non-blocking background export, and `TracerProvider` — per the
[OpenTelemetry Python instrumentation docs](https://opentelemetry.io/docs/languages/python/instrumentation/)
and the [SDK trace export reference](https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.export.html).
Content was rephrased for compliance with licensing restrictions.

## Architecture

```
                       backend startup (lifespan)
   ┌──────────────────────────────────────────────────────────────────┐
   │ tracing.init_tracing()                                            │
   │   • Resource(service.name="tokenquick-backend")                   │
   │   • TracerProvider                                                │
   │   • ALWAYS: BatchSpanProcessor(JsonlFileSpanExporter(             │
   │             logs/traces.jsonl))            ← zero-network default  │
   │   • IF TOKENQUICK_OTEL_EGRESS=="true" AND OTEL_EXPORTER_OTLP_      │
   │       ENDPOINT set: ALSO BatchSpanProcessor(OTLPSpanExporter(...)) │
   │       ← the ONE Sanctioned_Egress, opt-in, fail-closed            │
   │   • wrapped in try/except → WARNING, tracing disabled, app runs    │
   └───────────────────────────────┬──────────────────────────────────┘
                                   │ tracer = get_tracer()
                                   ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │ /api/chat generate (main.py, hand-instrumented)                    │
   │   with tracer.start_as_current_span("tokenquick.generate") as req: │
   │     req.set_attribute("session.id", session_id)                    │
   │     req.set_attribute("gen.phase", "generate")                     │
   │     … redaction span … cache span … compression span …             │
   │     … inference span (perform|build) … power span …                │
   │     (each: safe_span(...) helper; children of req)                 │
   └───────────────────────────────┬──────────────────────────────────┘
                                   ▼
              logs/traces.jsonl  (+ OTLP→collector iff gate on)
```

The JSONL logs (`audit_log`, `cache_log`, `power_log`) continue to be written
exactly as today; spans are sourced from the SAME in-memory values (dual-write).

## Components and Interfaces

### 1. `backend/tracing.py` — the tracing scaffold (Req 1, 2, 6)

A small, self-contained module so `main.py` wires tracing rather than embedding
provider construction. Public surface:

```python
def init_tracing() -> bool:
    """Initialize the global TracerProvider ONCE (idempotent). Returns True if
    tracing is active, False if it was disabled/failed. Never raises."""

def get_tracer() -> "Tracer":
    """Return the tokenquick tracer (a no-op tracer if init failed/disabled)."""

def shutdown_tracing() -> None:
    """Best-effort flush + shutdown of the provider (called on lifespan exit)."""
```

`init_tracing()`:
- Builds a `Resource` with `service.name = "tokenquick-backend"` (+ version).
- Constructs a `TracerProvider` and ALWAYS adds
  `BatchSpanProcessor(JsonlFileSpanExporter(DEFAULT_TRACE_PATH))` — the
  zero-network default (Req 1.2, 2.1). `BatchSpanProcessor` exports on a
  background thread, so no request blocks on I/O (Req 6.2).
- Reads the OTEL_Gate: `os.environ.get("TOKENQUICK_OTEL_EGRESS", "false").lower()
  == "true"`. ONLY when the gate is `true` AND `OTEL_EXPORTER_OTLP_ENDPOINT` is
  set does it ALSO add `BatchSpanProcessor(OTLPSpanExporter())` (Req 2.2, 2.3).
  When the gate is off, the OTLP branch never executes — fail closed to
  local-only, mirroring `POWER_TRY_MEASURED` (Req 2.4).
- Wrapped in `try/except`: on ANY failure it logs a WARNING and returns `False`;
  `get_tracer()` then returns a no-op tracer so call sites are unconditional and
  safe (Req 1.4, 6.1).

`DEFAULT_TRACE_PATH = backend/logs/traces.jsonl`; the module ensures the `logs/`
directory exists at init.

### 2. `JsonlFileSpanExporter` — the append-only default sink (Req 1.2, 7)

A custom `SpanExporter` (implements `export(self, spans) -> SpanExportResult` and
`shutdown()`), mirroring the append-only discipline of `power_log.py`/`cache_log.py`:

- Serializes each `ReadableSpan` to ONE JSON object per line (name, trace/span
  ids as hex, parent id, start/end unix-nanos, status, and the span's
  attributes dict) terminated by `\n`, appended under a `threading.Lock`, then
  flushed. File opened ONLY in append mode `'a'` — no truncate/rotate/clear.
- `export()` catches all exceptions, logs a WARNING, and returns
  `SpanExportResult.FAILURE` without raising (Req 6.1) — a failing exporter never
  breaks the app.
- It writes ONLY the attributes already on the span; it performs no enrichment
  and reads no HTTP headers/bodies (that safety comes from hand-instrumentation +
  the attribute discipline in §4/§5, not from this exporter).

This is a NEW file distinct from the three existing JSONL logs; it does not touch
or replace them (Req 7.1).

### 3. Hand-instrumentation of `/api/chat` generate (`main.py`) (Req 3, 4)

No FastAPI/ASGI auto-instrumentation is installed or enabled (Req: decision 3), so
no framework layer can copy request headers/bodies into spans. We add spans by
hand at the exact points the stages already run.

A tiny helper keeps call sites clean and failure-safe:

```python
@contextmanager
def safe_span(tracer, name, attributes=None):
    """Start a child span; on ANY error, yield a no-op and never raise (Req 6.1).
    Attributes are set via _set_safe_attrs (drops None, coerces scalars)."""
```

- **Request_Span** — `tokenquick.generate`, opened at the top of the generate
  branch AFTER the sessionId→400 guard. Attributes: `session.id`, `gen.phase =
  "generate"`, and (once known) `gen.task_mode = build|perform`. On exception,
  set span status ERROR + `record_exception` WITHOUT raw content (Req 3.2, 3.3).
- **Stage_Spans** (children of Request_Span, Req 3.4):
  - `tokenquick.redaction` — attributes mirror the redaction telemetry: per-
    category counts, `redaction.total`, `redaction.chars_redacted`,
    `redaction.latency_ms`, `stage.enabled`. Never raw/redacted text (Req 4.1).
  - `tokenquick.cache_lookup` — mirrors `cache_log.py`: `cache.decision`,
    `cache.top_score`, `cache.runner_up_score`, `cache.margin`,
    `cache.had_runner_up`, `stage.enabled` (Req 4.2).
  - `tokenquick.compression` — `compression.original_tokens`,
    `compression.compressed_tokens`, `compression.ratio`. No prompt text (Req 4.3).
  - `tokenquick.inference` — wraps `invoke_sync` (PERFORM) / `run_task_router`
    (BUILD): `inference.mode`, `inference.real_input_tokens`,
    `inference.real_output_tokens`, `inference.time_ms`. No code/prompt (Req 4.4).
  - `tokenquick.power` — mirrors `power_log.py`: `power.source`, `power.quality`,
    and — only when present/usable — `power.avg_watts`, `power.energy_joules`.
    When `quality == "unavailable"` (or a figure is `None`), the numeric
    attribute is OMITTED entirely (null-not-zero — Req 4.5, 5.2).
  - Disabled/unavailable stages set `stage.enabled=false` or
    `power.quality="unavailable"` rather than fabricated figures (Req 4.6).

Spans are emitted in ADDITION to the existing `audit_log`/`cache_log`/`power_log`
writes, from the same values (Req 7.2). No stage behavior, toggle, or streamed
annotation changes (Req 7.3).

### 4. Attribute discipline — the honesty enforcement point (Req 5)

All span attributes go through one choke point:

```python
_ALLOWED_ATTR_KEYS = { ... }  # explicit allowlist of scalar keys per §3
def _set_safe_attrs(span, attrs: dict) -> None:
    """Set ONLY allowlisted keys; drop any value that is None (preserves
    null-not-zero by ABSENCE); coerce to str/int/float/bool; never set a
    dict/list/object that could smuggle raw text."""
```

- **Allowlist, not denylist.** Only the scalar keys enumerated in §3 are ever
  set. There is no code path that copies a prompt, summary, redacted text,
  compressed text, or generated code onto a span (Req 5.1, 5.3).
- **Null-not-zero by absence.** A `None` figure is DROPPED (attribute absent),
  never coerced to `0` (Req 5.2). This matches how the JSONL logs write JSON
  `null`; on a span, "absent" is the analogue.
- **Attribute set ⊆ log fields.** The per-stage keys are a subset of what that
  stage's JSONL log already records, so tracing introduces no new sensitive
  field (Req 5.3).

### 5. Lifespan wiring + shutdown (Req 1.1, 6.3)

- In the existing `lifespan` asynccontextmanager: call `tracing.init_tracing()`
  during startup (after the other singletons), storing nothing global beyond what
  `tracing.py` owns. Log the resulting mode (local-only vs OTLP-enabled).
- After `yield` (shutdown), call `tracing.shutdown_tracing()` — best-effort
  flush + provider shutdown so buffered spans export without hanging shutdown
  (Req 6.3). Ordered alongside the existing `power_sampler.stop()`.

### 6. Dependencies (Req 1.3)

Add to `backend/requirements.txt`, pinned for Python 3.13:
- `opentelemetry-api`
- `opentelemetry-sdk`
- `opentelemetry-exporter-otlp-proto-http` (used ONLY when the gate is on; import
  is lazy/guarded so a missing collector never affects local-only default).

Exact pins chosen at implementation time against the installed interpreter; if a
pin fails to build, capture the error and stop (per the project's dependency
policy) rather than loosening it.

### 7. Documentation (Req 8)

README gets a "Distributed tracing (OpenTelemetry)" section: tracing defaults to
the append-only `logs/traces.jsonl` file exporter with **zero outbound calls**;
to view traces in a collector, set `TOKENQUICK_OTEL_EGRESS=true` and
`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces` (with a pointer to
running a local Jaeger/OTEL collector). Security note: this OTLP path is the ONE
outbound connection tracing can make and is OFF by default.

## Data models

`traces.jsonl` line shape (one JSON object per span):

```json
{
  "name": "tokenquick.cache_lookup",
  "trace_id": "<hex>", "span_id": "<hex>", "parent_span_id": "<hex|null>",
  "start_unix_nanos": 0, "end_unix_nanos": 0,
  "status": "OK|ERROR",
  "attributes": { "cache.decision": "hit", "cache.margin": 0.07, "stage.enabled": true }
}
```

Attributes are only the allowlisted scalars of §3/§4. No backend request/response
model changes.

## Error handling

- **Init failure** → WARNING, `init_tracing()` returns False, `get_tracer()`
  yields a no-op tracer; app serves normally (Req 1.4, 6.1).
- **Span/attr failure** → `safe_span` / `_set_safe_attrs` swallow and continue
  (Req 6.1).
- **Exporter failure** (`JsonlFileSpanExporter` or OTLP) → caught, WARNING,
  `SpanExportResult.FAILURE`; the batch processor drops the batch, request
  unaffected (Req 2.5, 6.1).
- **Shutdown** → best-effort flush, guarded (Req 6.3).

## Correctness Properties

Stated as invariants; verified per the Testing Strategy. The honesty invariant
(Property 1) is the load-bearing, mandatory one.

### Property 1: Span attribute honesty (zero raw value, null-not-zero)
For any generate request with adversarial raw inputs, no exported span's
attributes or events contain any ≥4-character substring of a raw prompt,
requirements summary, redacted text, compressed text, generated code, or secret;
and an unavailable power figure is ABSENT on the span, never `0`.

**Validates: Requirements 5.1, 5.2, 5.3, 5.4**

### Property 2: Local-only by default (gated egress)
When `TOKENQUICK_OTEL_EGRESS` is unset or not `"true"`, no OTLP exporter is
constructed and no OTLP endpoint is contacted; only the JSONL file exporter is
active.

**Validates: Requirements 2.1, 2.3, 2.4**

### Property 3: Non-blocking, never-crash instrumentation
Span creation, attribute setting, and export never raise into the request path;
a forced exporter/span failure still lets the generate request complete.

**Validates: Requirements 6.1, 6.2**

### Property 4: Additive dual-write
The three existing JSONL logs are still written with their existing shape and
append-only guarantees when tracing is enabled; span attributes for a stage are a
subset of that stage's log fields.

**Validates: Requirements 7.1, 7.2, 5.3**

## Testing Strategy

(Lean, per the project's testing posture.) The mandatory automated test is the
honesty check; the rest is lightweight unit/integration coverage.

- **`backend/tests/test_tracing_honesty.py` (MANDATORY, pytest):** register an
  in-memory span exporter (or a temp `JsonlFileSpanExporter` pointed at a
  tmp_path) on a test TracerProvider; drive the generate pipeline (or the span
  helpers directly) with adversarial raw prompts/secrets; collect all emitted
  spans; assert (a) no ≥4-char substring of any raw/sensitive input appears in
  any attribute value or event, and (b) for an `unavailable` power result the
  `power.avg_watts`/`power.energy_joules` attributes are ABSENT (never `0`).
- **Gate unit test:** with `TOKENQUICK_OTEL_EGRESS` unset/`false`, assert
  `init_tracing()` configures only the JSONL exporter and constructs no OTLP
  exporter (Property 2). With the gate `true` but no endpoint, assert it stays
  local-only.
- **Stage-span unit/integration test:** assert a generate run produces a parent
  `tokenquick.generate` span with the expected child stage spans and their
  scalar attributes (decision, quality, token counts), and that null figures are
  absent.
- **Never-crash test:** force the exporter to raise; assert the request still
  completes and no exception propagates (Property 3).
- Heavy property-based testing beyond Property 1 is not required.

## Resolved open questions

1. **Gate semantics** → `TOKENQUICK_OTEL_EGRESS` (default `false`); local-only
   when false; when true, OTLP via the standard `OTEL_EXPORTER_OTLP_ENDPOINT` as
   the single Sanctioned_Egress (§1).
2. **Default exporter** → custom append-only `JsonlFileSpanExporter` →
   `backend/logs/traces.jsonl` (not console), so terminal stays clean (§2).
3. **Instrumentation** → hand-instrumentation only; NO FastAPI
   auto-instrumentation, preventing header/body leakage into attributes (§3).
4. **Honesty test location** → `backend/tests/test_tracing_honesty.py`, asserting
   no ≥4-char raw substring in span attributes and unavailable power → null/None,
   never `0` (Testing Strategy, Property 1).
