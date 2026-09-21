"""
tracing.py — OpenTelemetry tracing scaffold (opentelemetry-tracing feature)

This module adds OpenTelemetry (OTEL) trace spans to the FastAPI backend as an
additive, identity-preserving layer over the existing JSONL telemetry logs
(``audit_log.py``, ``cache_log.py``, ``power_log.py``). It owns four things:

  * ``JsonlFileSpanExporter`` — a custom :class:`SpanExporter` that appends one
    JSON object per span to ``logs/traces.jsonl``, mirroring the append-only
    file discipline of ``power_log.py`` / ``cache_log.py`` EXACTLY: the file is
    opened only in append mode ('a'), one complete JSON object per line
    terminated by '\\n', serialized OUTSIDE a ``threading.Lock`` with only the
    write + flush under the lock, and every failure is caught → logged as a
    WARNING → returns ``SpanExportResult.FAILURE`` (never raises — Req 6.1).

  * ``init_tracing()`` — builds a single global ``TracerProvider`` ONCE
    (idempotent), ALWAYS wiring the zero-network ``JsonlFileSpanExporter`` as
    the default sink (Req 1.2, 2.1). It is best-effort: any failure logs a
    WARNING and returns ``False`` (Req 1.4, 6.1).

  * The **gated OTLP exporter** (the single Sanctioned_Egress, Req 2). Only when
    ``TOKENQUICK_OTEL_EGRESS == "true"`` AND ``OTEL_EXPORTER_OTLP_ENDPOINT`` is
    set does ``init_tracing`` LAZILY import the OTLP exporter and add a second
    batch processor. When the gate is off the OTLP package is NEVER imported and
    no endpoint is contacted — fail closed to local-only, mirroring the
    ``POWER_TRY_MEASURED`` precedent (Req 2.4).

  * The **attribute-safety choke point** (Req 5). ``_set_safe_attrs`` sets ONLY
    keys in the explicit ``_ALLOWED_ATTR_KEYS`` allowlist (plus the
    ``redaction.count.`` per-category prefix), DROPS any ``None`` value
    (null-not-zero by ABSENCE — never a fabricated ``0``), and coerces to
    str/int/float/bool only (a dict/list/other object is skipped entirely so no
    structure can smuggle raw text). ``safe_span`` yields a never-raising span
    context so instrumentation failures degrade silently (Req 6.1).

The three existing JSONL logs are untouched: this is a NEW parallel sink
(Req 7.1). Nothing here makes an outbound network call unless the OTEL_Gate is
explicitly enabled.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

logger = logging.getLogger(__name__)

# Service identity carried on every span's resource (Req 1.1).
_SERVICE_NAME = "tokenquick-backend"
_SERVICE_VERSION = "0.1.0"

# The tracer name used by every call site so instrumentation is consistent.
_TRACER_NAME = "tokenquick"

# Default append-only trace sink, computed relative to this file so it lives at
# backend/logs/traces.jsonl regardless of the process working directory.
_LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
DEFAULT_TRACE_PATH = os.path.join(_LOGS_DIR, "traces.jsonl")

# Best-effort: ensure logs/ exists at import. Guarded so a bad filesystem never
# crashes import — init_tracing() re-attempts and degrades to False on failure.
try:
    os.makedirs(_LOGS_DIR, exist_ok=True)
except Exception as err:  # noqa: BLE001 — never crash import over a log dir.
    logger.warning("Could not create trace log directory %r: %s", _LOGS_DIR, err)


# Module-level idempotency state. A second init_tracing() call is a no-op that
# returns the prior result (Req 1: initialize the provider exactly once).
_initialized = False
_init_result = False


def _status_str(span) -> str:
    """Map a ReadableSpan's status code to a plain "OK" / "ERROR" / "UNSET".

    Best-effort: any unexpected shape falls back to "UNSET" so serialization of
    one odd span never breaks the batch.
    """
    try:
        code = span.status.status_code
        # StatusCode is an IntEnum-like; compare by name to stay version-robust.
        name = getattr(code, "name", None)
        if name in ("OK", "ERROR", "UNSET"):
            return name
    except Exception:  # noqa: BLE001
        pass
    return "UNSET"


def _span_to_line(span) -> str:
    """Serialize one ReadableSpan to a single JSON line (terminated by '\\n').

    Emits ONLY the fields the design's data model enumerates: name, hex
    trace/span/parent ids, start/end unix-nanos, status, and the span's own
    attributes dict. It performs NO enrichment — it writes only the attributes
    already placed on the span by ``_set_safe_attrs`` (Req 7.1 / honesty).
    """
    ctx = span.context
    parent = span.parent
    entry = {
        "name": span.name,
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
        "parent_span_id": (
            format(parent.span_id, "016x") if parent is not None else None
        ),
        "start_unix_nanos": span.start_time,
        "end_unix_nanos": span.end_time,
        "status": _status_str(span),
        # span.attributes is a read-only mapping of the allowlisted scalars only.
        "attributes": dict(span.attributes or {}),
    }
    return json.dumps(entry, ensure_ascii=False) + "\n"


class JsonlFileSpanExporter(SpanExporter):
    """Append-only JSONL span exporter — the zero-network default sink (Req 1.2).

    Mirrors ``power_log.py`` / ``cache_log.py`` discipline: the file is opened
    ONLY in append mode ('a') — no truncate/rotate/clear — one complete JSON
    object per line, serialized outside a ``threading.Lock`` with only the write
    + flush under the lock. ``export`` catches ALL exceptions, logs a WARNING,
    and returns ``SpanExportResult.FAILURE`` without raising (Req 6.1) so a
    failing exporter never breaks the app.

    Like the JSONL logs, there is no persistent file handle: each export opens
    per-write, so ``shutdown`` has nothing to close.
    """

    def __init__(self, path: str = DEFAULT_TRACE_PATH):
        self._path = path
        # Guards the file write so concurrent exports never interleave partial
        # JSON lines (append-only integrity, matching the JSONL logs).
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        """Read-only path to the append-only trace file."""
        return self._path

    def export(self, spans) -> SpanExportResult:
        """Append each span as one JSON line; never raises (Req 6.1)."""
        try:
            # Serialize OUTSIDE the lock — only the write + flush are guarded.
            lines = [_span_to_line(span) for span in spans]
            payload = "".join(lines)
            with self._lock:
                # Append mode never truncates or rewrites prior lines.
                with open(self._path, "a", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
            return SpanExportResult.SUCCESS
        except Exception as err:  # noqa: BLE001 — a failing exporter never crashes.
            # Only allowlisted scalars are on the span, so nothing sensitive is
            # exposed by logging the failure.
            logger.warning("Trace spans could not be written to %r: %s", self._path, err)
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        """No-op: the file is opened per-write, so there is nothing to close."""
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        """Best-effort flush. Writes are already flushed per-export, so True."""
        return True


# --------------------------------------------------------------------------- #
# Attribute-safety choke point (Req 5) — the honesty enforcement point.
# --------------------------------------------------------------------------- #

# Explicit allowlist of the scalar attribute keys the design (§3) enumerates.
# Anything NOT in this set (or matching the redaction.count. prefix rule below)
# is DROPPED by _set_safe_attrs. This is an allowlist, not a denylist: there is
# no code path that can place a prompt / summary / redacted / compressed /
# generated value onto a span (Req 5.1, 5.3).
_ALLOWED_ATTR_KEYS = {
    # Request_Span
    "session.id",
    "gen.phase",
    "gen.task_mode",
    # generic stage toggle
    "stage.enabled",
    # redaction stage
    "redaction.total",
    "redaction.chars_redacted",
    "redaction.latency_ms",
    # cache-lookup stage
    "cache.decision",
    "cache.top_score",
    "cache.runner_up_score",
    "cache.margin",
    "cache.had_runner_up",
    # compression stage
    "compression.original_tokens",
    "compression.compressed_tokens",
    "compression.ratio",
    # inference stage
    "inference.mode",
    "inference.real_input_tokens",
    "inference.real_output_tokens",
    "inference.time_ms",
    # power-attribution stage
    "power.source",
    "power.quality",
    "power.avg_watts",
    "power.energy_joules",
}

# Per-category redaction counts arrive as keys like "redaction.count.email".
# They are allowlisted by PREFIX (only scalar counts follow the prefix).
_REDACTION_COUNT_PREFIX = "redaction.count."

# Value types that OTEL accepts as scalar attribute values. Anything else — a
# dict, list, or arbitrary object that could smuggle raw text — is skipped.
_ALLOWED_VALUE_TYPES = (str, bool, int, float)


def _is_allowed_key(key: str) -> bool:
    """True if ``key`` is explicitly allowlisted or a redaction.count.* count."""
    return key in _ALLOWED_ATTR_KEYS or key.startswith(_REDACTION_COUNT_PREFIX)


def _set_safe_attrs(span, attrs) -> None:
    """Set ONLY safe, allowlisted scalar attributes on ``span`` (Req 5).

    For each key/value:
      * SKIP the key if it is not allowlisted (allowing the ``redaction.count.``
        prefix) — an allowlist, so no unexpected field can ever be set.
      * SKIP any ``None`` value: null-not-zero by ABSENCE — a missing/unavailable
        figure drops the attribute rather than fabricating a ``0`` (Req 5.2).
      * Coerce to str/int/float/bool only. A dict/list/other object is SKIPPED
        entirely — never stringified, so no structure can smuggle raw text.

    Every per-attribute set is wrapped so a single bad attribute never raises
    into the caller (Req 6.1). ``span`` may be ``None`` (a no-op span from
    ``safe_span``), in which case this is a no-op.
    """
    if span is None or not attrs:
        return
    for key, value in attrs.items():
        try:
            if not isinstance(key, str) or not _is_allowed_key(key):
                continue
            # null-not-zero by absence: drop None, never coerce to 0.
            if value is None:
                continue
            # bool is a subclass of int; check it explicitly for clarity. Only
            # true scalars pass — dict/list/objects are skipped entirely.
            if isinstance(value, bool):
                safe_value = value
            elif isinstance(value, _ALLOWED_VALUE_TYPES):
                safe_value = value
            else:
                # Not a scalar (dict/list/other object) — never stringify it.
                continue
            span.set_attribute(key, safe_value)
        except Exception:  # noqa: BLE001 — one bad attr never breaks the span.
            continue


@contextmanager
def safe_span(tracer, name, attributes=None):
    """Start a child span named ``name``; NEVER raise into the caller (Req 6.1).

    Sets ``attributes`` through the ``_set_safe_attrs`` choke point, then yields
    the live span so callers can add more safe attributes / set status later
    (via ``_set_safe_attrs`` or a guarded ``span.set_status`` in their own
    try/except). On ANY error — a failed provider, a raising exporter, a bad
    tracer — this yields ``None`` (a no-op context) instead of propagating, so
    instrumentation can never degrade or crash the request path.
    """
    try:
        with tracer.start_as_current_span(name) as span:
            _set_safe_attrs(span, attributes or {})
            yield span
    except Exception as err:  # noqa: BLE001 — instrumentation is best-effort.
        logger.warning("safe_span %r failed; continuing without a span: %s", name, err)
        # Yield a no-op so the caller's ``with`` body still runs unharmed.
        yield None


# --------------------------------------------------------------------------- #
# Provider lifecycle (Req 1, 2, 6).
# --------------------------------------------------------------------------- #


def init_tracing() -> bool:
    """Initialize the global ``TracerProvider`` ONCE (idempotent).

    ALWAYS wires the zero-network ``JsonlFileSpanExporter`` via a
    ``BatchSpanProcessor`` (background export, so no request blocks — Req 6.2).
    When ``TOKENQUICK_OTEL_EGRESS == "true"`` AND ``OTEL_EXPORTER_OTLP_ENDPOINT``
    is set, ALSO lazily wires an OTLP exporter — the single Sanctioned_Egress
    (Req 2). Any failure logs a WARNING and returns ``False``; ``get_tracer``
    then yields a no-op tracer so call sites stay unconditional (Req 1.4, 6.1).

    Returns
    -------
    bool
        ``True`` when tracing is active (local-only or OTLP-enabled), ``False``
        when initialization was skipped or failed.
    """
    global _initialized, _init_result
    if _initialized:
        # A second call is a no-op returning the prior result (idempotent).
        return _init_result

    try:
        resource = Resource.create(
            {
                "service.name": _SERVICE_NAME,
                "service.version": _SERVICE_VERSION,
            }
        )
        provider = TracerProvider(resource=resource)

        # ALWAYS: the local-only, zero-network default sink (Req 1.2, 2.1).
        provider.add_span_processor(
            BatchSpanProcessor(JsonlFileSpanExporter(DEFAULT_TRACE_PATH))
        )

        otlp_enabled = _maybe_add_otlp_exporter(provider)

        trace.set_tracer_provider(provider)

        mode = "OTLP-enabled" if otlp_enabled else "local-only"
        logger.info("OpenTelemetry tracing initialized (%s).", mode)

        _init_result = True
    except Exception as err:  # noqa: BLE001 — startup never crashes over tracing.
        logger.warning("OpenTelemetry tracing failed to initialize: %s", err)
        _init_result = False
    finally:
        _initialized = True

    return _init_result


def _maybe_add_otlp_exporter(provider) -> bool:
    """Add the gated OTLP exporter iff the OTEL_Gate is on (Req 2).

    The gate is BOTH ``TOKENQUICK_OTEL_EGRESS == "true"`` AND a non-empty
    ``OTEL_EXPORTER_OTLP_ENDPOINT``. When the gate is off this returns ``False``
    immediately and the OTLP package is NEVER imported (fail-closed, mirroring
    ``POWER_TRY_MEASURED`` — Req 2.4). The lazy import + exporter construction is
    guarded: a missing package or bad endpoint logs a WARNING and leaves the
    local-only JSONL exporter active (Req 2.5).

    Returns ``True`` only when the OTLP exporter was successfully added.
    """
    gate_on = os.environ.get("TOKENQUICK_OTEL_EGRESS", "false").strip().lower() == "true"
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not (gate_on and endpoint):
        # Gate off (or no endpoint): DO NOT import the OTLP package at all.
        return False

    try:
        # LAZY import inside the gated branch ONLY — never at module top-level.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        logger.info(
            "OTLP span export enabled (Sanctioned_Egress) → %s", endpoint
        )
        return True
    except Exception as err:  # noqa: BLE001 — a bad OTLP setup never breaks init.
        logger.warning(
            "OTLP exporter could not be configured; staying local-only: %s", err
        )
        return False


def get_tracer():
    """Return the tokenquick tracer.

    When ``init_tracing`` failed or never ran, the OTEL API returns a NO-OP
    tracer by default, so call sites are always safe and never need to guard for
    a missing provider. This function never raises.
    """
    return trace.get_tracer(_TRACER_NAME)


def shutdown_tracing() -> None:
    """Best-effort flush + shutdown of the global provider (Req 6.3).

    Guards for a no-op provider that lacks ``force_flush`` / ``shutdown`` so it
    never hangs or raises on exit. Called from the FastAPI lifespan on shutdown.
    """
    try:
        provider = trace.get_tracer_provider()
    except Exception as err:  # noqa: BLE001
        logger.warning("Could not obtain tracer provider for shutdown: %s", err)
        return

    for method_name in ("force_flush", "shutdown"):
        method = getattr(provider, method_name, None)
        if method is None:
            # A no-op provider has no flush/shutdown — nothing to do.
            continue
        try:
            method()
        except Exception as err:  # noqa: BLE001 — shutdown is best-effort.
            logger.warning("Tracer provider %s failed: %s", method_name, err)
