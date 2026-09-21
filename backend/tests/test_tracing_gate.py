# Feature: opentelemetry-tracing, Task 9: gate-default + never-crash tests
"""
Task 9 (design Property 2 + Property 3): the lightweight, REQUIRED guards of the
local-first / never-crash identity invariants.

Property 2 — Local-only by default (gated egress). When ``TOKENQUICK_OTEL_EGRESS``
is unset/``false``, ``init_tracing()`` must configure ONLY the JSONL file
exporter and must NOT even import the OTLP transport package — the local-first
zero-network path has no runtime dependency on the OTLP transport. Validates
Requirements 2.1, 2.3, 2.4.

Property 3 — Non-blocking, never-crash instrumentation. Span creation, attribute
setting, and export never raise into the caller; a forced exporter/span failure
still lets the ``with safe_span(...)`` body run to completion. Validates
Requirements 6.1, 6.2.

## Why the gate assertion runs in a SUBPROCESS

``init_tracing()`` is idempotent via module-level ``_initialized`` /
``_init_result`` state, and OTEL only honors the FIRST ``set_tracer_provider``
per process. In the test process ``tracing`` may already be imported and
initialized (other tests import it), so we cannot observe a clean import state
in-process. Instead we spawn a fresh interpreter with a controlled ``env`` and
assert, from a virgin import state, that ``opentelemetry.exporter.otlp`` is NOT
in ``sys.modules`` after ``init_tracing()`` — the load-bearing proof that the
gate-off path never imports the transport package.
"""

import os
import subprocess
import sys

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from tracing import safe_span, _set_safe_attrs


# --------------------------------------------------------------------------- #
# Helpers for the subprocess gate tests (Property 2).
# --------------------------------------------------------------------------- #

# backend/ dir so the child interpreter can `import tracing` exactly like the
# test process does (mirrors conftest.py's sys.path insertion).
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Script run in a fresh interpreter: init tracing, then assert the OTLP transport
# package was never imported. Prints OK on success so the parent can assert it.
_GATE_OFF_SCRIPT = (
    "import sys; import tracing; "
    "res = tracing.init_tracing(); "
    'assert "opentelemetry.exporter.otlp" not in sys.modules, '
    '"OTLP imported when gate off"; '
    'print("OK", res)'
)

# Gate true + a dummy endpoint: init should return True and not crash. We do NOT
# assert a real export (no collector is running); the point is "doesn't crash".
_GATE_ON_DUMMY_SCRIPT = (
    "import sys; import tracing; "
    "res = tracing.init_tracing(); "
    'print("OK", res)'
)


def _run_child(script: str, env: dict) -> subprocess.CompletedProcess:
    """Run ``script`` in a fresh interpreter with ``env``, cwd = backend/.

    ``PYTHONPATH`` is set to the backend dir so ``import tracing`` resolves the
    real module under test in the child, independent of the parent's sys.path.
    """
    child_env = dict(env)
    existing_pp = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = (
        _BACKEND_DIR + (os.pathsep + existing_pp if existing_pp else "")
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        env=child_env,
        cwd=_BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _base_env_without_gate() -> dict:
    """A copy of the current env with BOTH gate vars explicitly removed."""
    env = dict(os.environ)
    env.pop("TOKENQUICK_OTEL_EGRESS", None)
    env.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    return env


# --------------------------------------------------------------------------- #
# 9a. Gate default = local-only (Property 2, Req 2.1/2.3/2.4)
# --------------------------------------------------------------------------- #


def test_gate_off_does_not_import_otlp_package():
    """Req 2.1, 2.4 — the load-bearing gate assertion. With the gate unset,
    ``init_tracing()`` stays local-only and NEVER imports the OTLP transport
    package (proven from a clean import state in a subprocess)."""
    env = _base_env_without_gate()
    result = _run_child(_GATE_OFF_SCRIPT, env)
    assert result.returncode == 0, (
        f"gate-off child failed: rc={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "OK" in result.stdout, f"unexpected stdout: {result.stdout!r}"


def test_gate_off_explicit_false_does_not_import_otlp_package():
    """Req 2.1, 2.4 — an explicit ``TOKENQUICK_OTEL_EGRESS=false`` behaves the
    same as unset: local-only, OTLP transport never imported."""
    env = _base_env_without_gate()
    env["TOKENQUICK_OTEL_EGRESS"] = "false"
    result = _run_child(_GATE_OFF_SCRIPT, env)
    assert result.returncode == 0, (
        f"gate-false child failed: rc={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "OK" in result.stdout, f"unexpected stdout: {result.stdout!r}"


def test_gate_true_but_no_endpoint_stays_local_only():
    """Req 2.3, 2.4 — the scaffold requires BOTH gate ``true`` AND a non-empty
    endpoint. With the gate ``true`` but NO endpoint, it stays local-only and
    STILL never imports the OTLP transport package (fail-closed)."""
    env = _base_env_without_gate()
    env["TOKENQUICK_OTEL_EGRESS"] = "true"
    # OTEL_EXPORTER_OTLP_ENDPOINT deliberately left unset by _base_env_without_gate.
    result = _run_child(_GATE_OFF_SCRIPT, env)
    assert result.returncode == 0, (
        f"gate-true-no-endpoint child failed: rc={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "OK" in result.stdout, f"unexpected stdout: {result.stdout!r}"


def test_gate_true_with_dummy_endpoint_does_not_crash():
    """Req 2.2 (lenient) — gate ``true`` + a dummy endpoint: ``init_tracing()``
    returns True and does not crash. We do NOT assert a real export (no collector
    is running); the point is that enabling the Sanctioned_Egress is robust."""
    env = _base_env_without_gate()
    env["TOKENQUICK_OTEL_EGRESS"] = "true"
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://127.0.0.1:4318/v1/traces"
    result = _run_child(_GATE_ON_DUMMY_SCRIPT, env)
    assert result.returncode == 0, (
        f"gate-on-dummy child failed: rc={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "OK True" in result.stdout, f"unexpected stdout: {result.stdout!r}"


# --------------------------------------------------------------------------- #
# 9b. Never-crash instrumentation (Property 3, Req 6.1/6.2)
# --------------------------------------------------------------------------- #


class _RaisingSpanExporter(SpanExporter):
    """A SpanExporter whose ``export`` always RAISES — used to prove that a
    failing exporter never propagates an exception into the request path."""

    def export(self, spans):  # noqa: ARG002
        raise RuntimeError("boom: exporter deliberately failing")

    def shutdown(self):
        return None


class _RaisingTracer:
    """A dummy tracer whose ``start_as_current_span`` raises, to prove
    ``safe_span`` yields a usable no-op even when the tracer itself blows up."""

    def start_as_current_span(self, name):  # noqa: ARG002
        raise RuntimeError("boom: tracer deliberately failing")


def test_export_failure_does_not_propagate():
    """Req 6.1 — a SpanExporter whose ``export`` raises must not surface an
    exception into the caller. Drive ``safe_span`` + ``_set_safe_attrs`` through
    a real provider wired to the raising exporter (SimpleSpanProcessor exports
    synchronously on span end, so the failure fires inside the ``with`` block's
    exit) and assert the body ran and nothing propagated."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_RaisingSpanExporter()))
    tracer = provider.get_tracer("tokenquick")

    body_ran = False
    try:
        with safe_span(
            tracer,
            "tokenquick.generate",
            {"session.id": "s1", "gen.phase": "generate"},
        ) as span:
            _set_safe_attrs(span, {"gen.task_mode": "build"})
            body_ran = True
    except Exception as err:  # pragma: no cover — the whole point is this never fires.
        provider.shutdown()
        raise AssertionError(
            f"safe_span leaked an exception on export failure: {err!r}"
        )
    finally:
        # Provider shutdown also flushes; guard it so a raising exporter here
        # doesn't mask the assertion above.
        try:
            provider.shutdown()
        except Exception:  # noqa: BLE001
            pass

    assert body_ran, "with safe_span(...) body did not execute"


def test_safe_span_yields_noop_when_tracer_raises():
    """Req 6.1 — if the tracer's ``start_as_current_span`` itself raises,
    ``safe_span`` yields a no-op (``None``) and the ``with`` body STILL runs; no
    exception escapes."""
    body_ran = False
    yielded = "unset"
    try:
        with safe_span(_RaisingTracer(), "tokenquick.generate", {"session.id": "s1"}) as span:
            yielded = span
            body_ran = True
    except Exception as err:  # pragma: no cover
        raise AssertionError(
            f"safe_span leaked an exception when tracer raised: {err!r}"
        )

    assert body_ran, "with safe_span(...) body did not execute on tracer failure"
    assert yielded is None, "safe_span should yield a None no-op when the tracer raises"


def test_set_safe_attrs_on_none_span_is_noop():
    """Req 6.1 — ``_set_safe_attrs(None, {...})`` (the no-op-span case produced
    by a failed ``safe_span``) is a harmless no-op and never raises."""
    # Should simply return without raising.
    _set_safe_attrs(None, {"session.id": "s1", "power.avg_watts": 12.5})
    _set_safe_attrs(None, {})
    _set_safe_attrs(None, None)
