# Feature: opentelemetry-tracing, Task 10: stage-span integration test
"""
Task 10 (OPTIONAL, design Testing Strategy "stage-span unit/integration test").

Asserts that a generate run produces the parent ``tokenquick.generate`` span with
its five child stage spans (``tokenquick.redaction``, ``tokenquick.cache_lookup``,
``tokenquick.compression``, ``tokenquick.inference``, ``tokenquick.power``)
correctly parented under it, carrying representative scalar attributes, and that
an ``unavailable`` power result omits the numeric power figures (null-not-zero by
absence — dual-write agrees with the JSONL logs). Validates Requirements 3.1,
3.4, 4.1, 4.2, 4.4, 4.5, 7.2.

## Approach: representative spans, not the real /api/chat handler

Per the design's lean posture and the brief, this test does NOT drive the real
``/api/chat`` generate handler (which needs loaded models + mocking of
``invoke_sync`` / ``run_task_router``). Instead it builds the same span SHAPE the
handler builds via the REAL ``tracing`` helpers: it opens the parent span the way
``main._start_generate_span`` does (``start_span`` + ``_set_safe_attrs``), then
opens each child under it the way ``main._child_span`` does — using
``opentelemetry.trace.use_span(parent, end_on_exit=False)`` so the child nests
correctly — with the representative scalar attribute dicts the stage helpers
(``_redaction_span_attrs`` / ``_cache_span_attrs`` / ``_compression_span_attrs`` /
``_power_span_attrs``) produce. This is an acceptable integration-level check per
the design; it exercises the real allowlist/parenting code without the model
dependency.

Capture uses the same in-memory approach as ``test_tracing_honesty.py``: a fresh
``TracerProvider`` + ``InMemorySpanExporter``, tracer passed straight into the
helpers, so it is independent of any global provider already set in the process.
"""

import pytest

from opentelemetry import trace as _otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from tracing import safe_span, _set_safe_attrs


@pytest.fixture
def provider_and_exporter():
    """A fresh, isolated TracerProvider whose spans land in memory.

    Mirrors the honesty test's fixture: we do NOT call
    ``trace.set_tracer_provider`` — the tracer from this provider is handed
    straight to the helpers, so capture is independent of global provider state.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("tokenquick")
    try:
        yield tracer, exporter
    finally:
        provider.shutdown()


# Child span names, in the pipeline order the handler emits them.
_STAGE_SPAN_NAMES = [
    "tokenquick.redaction",
    "tokenquick.cache_lookup",
    "tokenquick.compression",
    "tokenquick.inference",
    "tokenquick.power",
]


def _build_generate_trace(tracer, power_attrs):
    """Build the parent generate span + five child stage spans, mirroring the
    handler's parenting (``use_span(parent, end_on_exit=False)``) and the stage
    helpers' representative scalar attribute dicts.

    ``power_attrs`` is injected so callers can exercise both the usable-figures
    and the ``unavailable`` (null-not-zero) power cases.
    """
    # Parent, opened the way main._start_generate_span does.
    parent = tracer.start_span("tokenquick.generate")
    _set_safe_attrs(parent, {"session.id": "sess-int-1", "gen.phase": "generate"})
    try:
        with _otel_trace.use_span(parent, end_on_exit=False):
            # redaction — per-category counts + totals (mirrors _redaction_span_attrs).
            with safe_span(
                tracer,
                "tokenquick.redaction",
                {
                    "stage.enabled": True,
                    "redaction.total": 2,
                    "redaction.chars_redacted": 24,
                    "redaction.latency_ms": 3.4,
                    "redaction.count.email": 1,
                    "redaction.count.ssn": 1,
                },
            ):
                pass

            # cache_lookup — mirrors _cache_span_attrs (a miss, so compression runs).
            with safe_span(
                tracer,
                "tokenquick.cache_lookup",
                {
                    "stage.enabled": True,
                    "cache.decision": "miss",
                    "cache.top_score": 0.41,
                    "cache.runner_up_score": 0.33,
                    "cache.margin": 0.08,
                    "cache.had_runner_up": True,
                },
            ):
                pass

            # compression — scalar token metrics (mirrors _compression_span_attrs).
            with safe_span(
                tracer,
                "tokenquick.compression",
                {
                    "stage.enabled": True,
                    "compression.original_tokens": 140,
                    "compression.compressed_tokens": 105,
                    "compression.ratio": 0.75,
                },
            ):
                pass

            # inference — scalar token/timing metadata (mirrors task 7.3).
            with safe_span(
                tracer,
                "tokenquick.inference",
                {
                    "inference.mode": "build",
                    "inference.real_input_tokens": 105,
                    "inference.real_output_tokens": 260,
                    "inference.time_ms": 903.7,
                },
            ):
                pass

            # power — mirrors _power_span_attrs; power_attrs varies per test.
            with safe_span(tracer, "tokenquick.power", power_attrs):
                pass
    finally:
        # The handler ends the parent in its generator's finally; do the same.
        parent.end()


def _spans_by_name(exporter):
    return {s.name: s for s in exporter.get_finished_spans()}


def test_generate_trace_has_parent_and_five_parented_children(provider_and_exporter):
    """Req 3.1, 3.4, 4.1, 4.2, 4.4 — the parent ``tokenquick.generate`` span
    exists with ``gen.phase="generate"``, and all five child stage spans exist
    and are parented under it (verified via parent span_id linkage)."""
    tracer, exporter = provider_and_exporter

    # A usable (estimated) power result for this shape check.
    _build_generate_trace(
        tracer,
        {
            "power.source": "utilization-estimate",
            "power.quality": "estimated",
            "power.avg_watts": 14.2,
            "power.energy_joules": 41.6,
        },
    )

    spans = exporter.get_finished_spans()
    by_name = _spans_by_name(exporter)

    # Parent + five children captured.
    assert len(spans) == 6, f"expected 6 spans, got {len(spans)}: {[s.name for s in spans]}"

    parent = by_name["tokenquick.generate"]
    assert dict(parent.attributes or {})["gen.phase"] == "generate"
    assert dict(parent.attributes or {})["session.id"] == "sess-int-1"
    # The parent is a root span (no parent).
    assert parent.parent is None, "generate span should be the trace root"

    parent_span_id = parent.context.span_id
    parent_trace_id = parent.context.trace_id

    # Every stage child exists and is parented under the generate span.
    for name in _STAGE_SPAN_NAMES:
        assert name in by_name, f"missing child span {name!r}"
        child = by_name[name]
        assert child.parent is not None, f"{name} should have a parent"
        assert child.parent.span_id == parent_span_id, (
            f"{name} is not parented under tokenquick.generate "
            f"(parent span_id {child.parent.span_id:016x} != "
            f"{parent_span_id:016x})"
        )
        # Same trace as the parent.
        assert child.context.trace_id == parent_trace_id, (
            f"{name} is not in the same trace as the parent"
        )


def test_stage_children_carry_representative_scalar_attrs(provider_and_exporter):
    """Req 4.1, 4.2, 4.4 — the child stage spans carry the representative scalar
    attributes (decision, token counts, ratios) the JSONL logs record."""
    tracer, exporter = provider_and_exporter

    _build_generate_trace(
        tracer,
        {
            "power.source": "powermetrics",
            "power.quality": "measured",
            "power.avg_watts": 11.0,
            "power.energy_joules": 22.0,
        },
    )

    by_name = _spans_by_name(exporter)

    cache = dict(by_name["tokenquick.cache_lookup"].attributes or {})
    assert cache["cache.decision"] == "miss"
    assert cache["cache.had_runner_up"] is True

    compression = dict(by_name["tokenquick.compression"].attributes or {})
    assert compression["compression.original_tokens"] == 140
    assert compression["compression.ratio"] == 0.75

    inference = dict(by_name["tokenquick.inference"].attributes or {})
    assert inference["inference.real_output_tokens"] == 260
    assert inference["inference.mode"] == "build"


def test_unavailable_power_omits_numeric_figures(provider_and_exporter):
    """Req 4.5 — an ``unavailable`` power result omits ``power.avg_watts`` and
    ``power.energy_joules`` (null-not-zero by ABSENCE), while the honest
    ``power.quality`` / ``power.source`` scalars remain. Dual-write agrees with
    the null-not-zero JSONL behavior."""
    tracer, exporter = provider_and_exporter

    # Mirrors an unavailable/disabled attribution: numeric figures are None, which
    # _set_safe_attrs DROPS (never coerces to 0).
    _build_generate_trace(
        tracer,
        {
            "power.source": "utilization-estimate",
            "power.quality": "unavailable",
            "power.avg_watts": None,
            "power.energy_joules": None,
        },
    )

    power = dict(_spans_by_name(exporter)["tokenquick.power"].attributes or {})

    # Numeric figures ABSENT — never a fabricated 0.
    assert "power.avg_watts" not in power
    assert "power.energy_joules" not in power
    # Honest scalars remain.
    assert power["power.quality"] == "unavailable"
    assert power["power.source"] == "utilization-estimate"
