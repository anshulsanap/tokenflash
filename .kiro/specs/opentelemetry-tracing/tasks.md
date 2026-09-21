# Implementation Plan: OpenTelemetry Tracing

## Overview

This plan converts the opentelemetry-tracing design into incremental, bottom-up
coding steps that mirror the existing backend patterns (a small self-contained
module wired — not embedded — into `main.py`; append-only file discipline like
`power_log.py`/`cache_log.py`; best-effort, never-crash failure handling; and
the honesty invariants of the other stages). Each step ends by wiring new code
into the running system so nothing is orphaned.

The feature adds four backend pieces: the OTEL dependencies, a `tracing.py`
scaffold (tracer provider + gated exporters + no-op-safe accessor), a custom
append-only `JsonlFileSpanExporter`, and hand-instrumentation of the `/api/chat`
generate pipeline (one parent span + five child stage spans). It also adds the
mandatory honesty test and README docs. NO frontend changes; NO change to the
three existing JSONL logs, the stage toggles, or the streamed annotations.

**Test policy (per the project's lean posture):** the MANDATORY automated test is
the honesty check (`test_tracing_honesty.py`, design Property 1). The gate
default-local test and the never-crash test are lightweight REQUIRED checks
(they guard load-bearing identity/robustness invariants). Any heavier or
nice-to-have tests are marked OPTIONAL/deferrable with the `- [ ]*` convention.

## Tasks

- [x] 1. Add the OpenTelemetry dependencies
  - Edit `backend/requirements.txt`: add `opentelemetry-api`, `opentelemetry-sdk`,
    and `opentelemetry-exporter-otlp-proto-http`, pinned to versions compatible
    with the installed Python 3.13 interpreter. Add a comment block explaining
    that the OTLP exporter is used ONLY when `TOKENQUICK_OTEL_EGRESS=true` (the
    single Sanctioned_Egress) and is otherwise never imported at runtime.
  - Install into `backend/.venv` and verify import of `opentelemetry.sdk.trace`.
    If a pin fails to build/resolve, capture the error log and STOP (do not
    loosen the pin) per the project's dependency policy.
  - _Requirements: 1.3_

- [x] 2. Implement the append-only JSONL span exporter (`backend/tracing.py`)
  - In a new `backend/tracing.py`, implement `JsonlFileSpanExporter` as a custom
    `opentelemetry.sdk.trace.export.SpanExporter`: `export(self, spans) ->
    SpanExportResult` and `shutdown(self)`. Mirror `power_log.py` discipline:
    serialize each `ReadableSpan` to ONE JSON object per line (name, hex
    trace_id/span_id, parent_span_id-or-null, start/end unix-nanos, status, and
    the span's attributes dict), append under a `threading.Lock`, flush; file
    opened ONLY in append mode `'a'` — no truncate/rotate/clear.
  - `DEFAULT_TRACE_PATH = backend/logs/traces.jsonl`; ensure the `logs/` dir
    exists. `export()` catches all exceptions, logs a WARNING, returns
    `SpanExportResult.FAILURE` (never raises — Req 6.1). Writes ONLY the
    attributes already on the span (no enrichment, reads no headers/bodies).
  - _Requirements: 1.2, 6.1, 7.1_

- [x] 3. Implement the tracing scaffold in `backend/tracing.py` (provider + gate)
  - [x] 3.1 Provider init with the local-only default exporter
    - `init_tracing() -> bool`: build a `Resource` with
      `service.name="tokenquick-backend"`, construct a `TracerProvider`, and
      ALWAYS add `BatchSpanProcessor(JsonlFileSpanExporter(DEFAULT_TRACE_PATH))`
      (zero-network default, background export so nothing blocks). Set it as the
      global tracer provider. Idempotent (a second call is a no-op). Wrap the
      whole thing in try/except → WARNING → return False on any failure.
    - `get_tracer()` returns the tokenquick tracer, or a NO-OP tracer when init
      failed/was disabled, so call sites are unconditional and safe.
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 6.1_
  - [x] 3.2 Gated OTLP exporter (the Sanctioned_Egress)
    - Read `TOKENQUICK_OTEL_EGRESS` (default `"false"`). ONLY when it equals
      `"true"` AND `OTEL_EXPORTER_OTLP_ENDPOINT` is set: LAZILY import
      `opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter`
      and add a second `BatchSpanProcessor(OTLPSpanExporter())`. When the gate is
      off, this branch NEVER executes and the OTLP package is NEVER imported
      (fail-closed, mirroring `POWER_TRY_MEASURED`).
    - A guarded OTLP import failure (package missing) logs a WARNING and leaves
      the local-only exporter active — it never breaks init.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_
  - [x] 3.3 Shutdown + the attribute-safety choke point
    - `shutdown_tracing()`: best-effort flush + provider shutdown, guarded so it
      never hangs/raises on exit (Req 6.3).
    - Implement `_set_safe_attrs(span, attrs)` and a `safe_span(tracer, name,
      attributes=None)` contextmanager: `safe_span` starts a child span and
      yields a no-op on any error (never raises — Req 6.1). `_set_safe_attrs`
      sets ONLY keys in an explicit `_ALLOWED_ATTR_KEYS` allowlist, DROPS any
      `None` value (null-not-zero by absence), and coerces to str/int/float/bool
      (never sets a dict/list/object that could smuggle raw text).
    - _Requirements: 5.1, 5.2, 5.3, 6.1, 6.3_

- [x] 4. Write the MANDATORY honesty property test (`backend/tests/test_tracing_honesty.py`) — REQUIRED, not deferrable
  - **MANDATORY — the load-bearing test for this feature** (design Property 1).
    Register an in-memory span exporter (or a temp `JsonlFileSpanExporter` at a
    `tmp_path`) on a test `TracerProvider`. Drive the span helpers / stage
    attribute construction with ADVERSARIAL raw prompts + secrets (SSN, email,
    API-key-like strings, a long unique sentinel). Collect ALL emitted spans and
    assert:
    1. No ≥4-character substring of ANY raw/sensitive input appears in ANY span
       attribute value or span event (across all spans).
    2. For an `unavailable` power result, the `power.avg_watts` /
       `power.energy_joules` attributes are ABSENT (never `0`, never present).
  - Exercise the REAL `_set_safe_attrs` / `safe_span` / stage-attribute code (not
    mocks of them). Tag the test with a feature comment.
  - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [x] 5. Checkpoint - scaffold + exporter + honesty test complete
  - Ensure the honesty test passes and the backend suite is green; ask the user
    if questions arise.

- [x] 6. Initialize + shut down tracing in the FastAPI lifespan (`backend/main.py`)
  - In the existing `lifespan` asynccontextmanager: call `tracing.init_tracing()`
    during startup (after the other singletons) and log the resulting mode
    (local-only vs OTLP-enabled). After `yield`, call `tracing.shutdown_tracing()`
    (best-effort), ordered alongside the existing `power_sampler.stop()`.
    Import the tracing module with the other stage imports.
  - Do NOT construct spans here; this task only wires init/shutdown.
  - _Requirements: 1.1, 6.3_

- [x] 7. Hand-instrument the generate request span + stage spans (`backend/main.py`)
  - [x] 7.1 Parent Request_Span for the generate pipeline
    - In the generate branch, AFTER the existing sessionId→400 guard, open the
      parent span `tokenquick.generate` via `safe_span(get_tracer(), ...)`
      wrapping the request work. Attributes: `session.id`, `gen.phase="generate"`,
      and `gen.task_mode` (set once the intent is classified). On exception, set
      span status ERROR + record_exception WITHOUT raw content. Do NOT feed raw
      prompt/summary/generated text.
    - _Requirements: 3.1, 3.2, 3.3, 3.4_
  - [x] 7.2 Redaction + cache + compression child spans
    - Add child `tokenquick.redaction` (per-category counts, `redaction.total`,
      `redaction.chars_redacted`, `redaction.latency_ms`, `stage.enabled`),
      `tokenquick.cache_lookup` (mirrors `cache_log`: `cache.decision`,
      `cache.top_score`, `cache.runner_up_score`, `cache.margin`,
      `cache.had_runner_up`, `stage.enabled`), and `tokenquick.compression`
      (`compression.original_tokens`, `compression.compressed_tokens`,
      `compression.ratio`). All attributes via `_set_safe_attrs`; no raw/redacted/
      compressed text. Disabled stages set `stage.enabled=false` (Req 4.6).
    - _Requirements: 4.1, 4.2, 4.3, 4.6, 5.1, 5.2, 5.3, 7.2, 7.3_
  - [x] 7.3 Inference + power child spans
    - Add `tokenquick.inference` around `invoke_sync` (PERFORM) and
      `run_task_router` (BUILD): `inference.mode`, `inference.real_input_tokens`,
      `inference.real_output_tokens`, `inference.time_ms`. Add `tokenquick.power`
      mirroring `power_log`: `power.source`, `power.quality`, and — ONLY when
      usable — `power.avg_watts`, `power.energy_joules` (OMITTED when `None`/
      `unavailable`, null-not-zero by absence). Emit spans IN ADDITION to the
      existing log writes (dual-write), from the same values.
    - _Requirements: 4.4, 4.5, 4.6, 5.2, 7.2, 7.3_

- [x] 8. Checkpoint - full pipeline instrumented
  - Ensure all tests pass and the backend suite is green; ask the user if
    questions arise.

- [x] 9. Add the gate-default and never-crash tests (lightweight, REQUIRED)
  - Gate test: with `TOKENQUICK_OTEL_EGRESS` unset/`false`, assert `init_tracing`
    configures ONLY the JSONL exporter and constructs NO OTLP exporter / imports
    no OTLP package (design Property 2). With the gate `true` but no endpoint,
    assert it stays local-only.
  - Never-crash test: force the exporter (or a span op) to raise; assert the
    request/pipeline still completes and no exception propagates (Property 3).
  - _Requirements: 2.1, 2.3, 2.4, 6.1, 6.2_

- [x]* 10. Optional: stage-span integration test
  - OPTIONAL/deferrable. Drive a generate run (mocked model) and assert a parent
    `tokenquick.generate` span with the expected child stage spans and their
    scalar attributes exists, and that null power figures are absent (dual-write
    agrees with the JSONL logs).
  - _Requirements: 3.1, 3.4, 4.1, 4.2, 4.4, 4.5, 7.2_

- [x] 11. Document the opt-in collector path in the README
  - Add a "Distributed tracing (OpenTelemetry)" section: tracing defaults to the
    append-only `logs/traces.jsonl` file exporter with ZERO outbound calls; to
    view traces in a collector, set `TOKENQUICK_OTEL_EGRESS=true` and
    `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces`, with a pointer
    to running a local Jaeger/OTEL collector. Security note: this OTLP path is the
    ONE outbound connection tracing can make and is OFF by default.
  - _Requirements: 8.1, 8.2_

- [x] 12. Final checkpoint - ensure all tests pass
  - Ensure the mandatory honesty test, the gate + never-crash tests, and the full
    backend suite pass; ask the user if questions arise.

## Notes

- **Test policy (lean, per directive):** only the honesty test (Task 4) is the
  load-bearing MANDATORY check; the gate-default and never-crash tests (Task 9)
  are lightweight REQUIRED guards of the identity/robustness invariants. The
  stage-span integration test (Task 10) is `- [ ]*` optional. No heavy
  property-based testing beyond the honesty check.
- **Additive dual-write:** the three existing JSONL logs (`audit_log`,
  `cache_log`, `power_log`) are untouched and keep their append-only + zero-raw
  guarantees; spans are emitted from the SAME in-memory values.
- **Null-not-zero on spans = attribute ABSENCE** (OTEL attributes cannot be
  null); a `None` figure drops the key, never a `0`.
- **Gate is fail-closed:** with `TOKENQUICK_OTEL_EGRESS` off, the OTLP package is
  never imported and no endpoint is contacted — the local-first zero-network path
  has no runtime dependency on external transport packages.
- **Hand-instrumentation only:** no FastAPI/ASGI auto-instrumentation, so no
  request headers or bodies can leak into span attributes.
- Checkpoints (Tasks 5, 8, 12) provide incremental validation at natural breaks.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["2"] },
    { "id": 2, "tasks": ["3.1", "3.2", "3.3"] },
    { "id": 3, "tasks": ["4"] },
    { "id": 4, "tasks": ["5"] },
    { "id": 5, "tasks": ["6"] },
    { "id": 6, "tasks": ["7.1", "7.2", "7.3"] },
    { "id": 7, "tasks": ["8"] },
    { "id": 8, "tasks": ["9", "10", "11"] },
    { "id": 9, "tasks": ["12"] }
  ]
}
```
