"""
AI FinOps Router — FastAPI Backend
Vercel AI SDK Data Stream Protocol
https://sdk.vercel.ai/docs/ai-sdk-ui/stream-protocol
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

from llm_provider import stream_chat, stream_response, invoke_sync, PROVIDER
from compressor import compress_prompt, compress_prompt_detailed
from task_router import run_task_router

# ── Pre-inference redaction stage (feature: pre-inference-redaction) ────────
# Build on the existing, tested redaction modules — import and wire them; do
# NOT reimplement any of them here.
from redactor import (
    BUILTIN_REGEX_DETECTORS,
    CATEGORIES,
    NerDetector,
    load_ner_model,
    redact,
    verify_placeholders,
)
from custom_terms import CustomTermDetector, CustomTermsStore
from audit_log import AuditLog
from redaction_state import state as redaction_state

# ── Hardened semantic-cache stage (feature: hardened-semantic-cache) ────────
# Reuse the tested cache modules — import and wire them; do NOT reimplement.
import time
from datetime import datetime, timezone

import semantic_cache
from cache_state import state as cache_state
from cache_log import CacheLog

# ── Hardware power / energy tracking stage (feature: hardware-power-tracking) ─
# Reuse the tested power modules — import and wire them; do NOT reimplement.
from power_source import detect_power_source
from power_sampler import (
    PowerSampler,
    PowerAttribution,
    disabled_attribution,
    unavailable_attribution,
)
from power_state import state as power_state
from power_log import PowerLog

# ── Private on-device artifacts stage (feature: private-on-device-artifacts) ─
# In-memory toggle for the Sandpack live-preview stage. Backend touch is limited
# to (a) injecting the Artifact_Delimiter instruction on the BUILD path when the
# stage is enabled, and (b) the independent toggle endpoints. No backend code
# ever compiles or executes generated artifact code (design §1).
from artifact_state import state as artifact_state

# ── OpenTelemetry tracing stage (feature: opentelemetry-tracing) ────────────
# Additive, identity-preserving trace spans over the SAME scalars the JSONL logs
# already record. The scaffold owns provider construction + the attribute-safety
# choke point; main.py only wires init/shutdown (Task 6) and hand-instruments
# the generate pipeline (Task 7). All span ops are best-effort — no span
# operation may raise into the request/stream path (Req 6.1).
import tracing
from opentelemetry import trace as _otel_trace
from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redaction stage singletons (built ONCE at startup, not per request).
#
# These module-level globals are populated by the FastAPI lifespan handler
# below (task 11.5). The NER model and the custom-terms store are expensive to
# construct, so they are created exactly once at process startup and reused for
# the life of the process (Req 3.4). The only per-request touch of the store is
# a cheap ``maybe_reload()`` mtime check inside the generate handler (Req 4.6).
# ---------------------------------------------------------------------------

ner_detector: NerDetector | None = None
custom_terms_store: CustomTermsStore | None = None
custom_term_detector: CustomTermDetector | None = None
audit_log: AuditLog | None = None

# ---------------------------------------------------------------------------
# Semantic-cache stage singletons (built ONCE at startup by the lifespan
# handler below). The embedding model and Chroma collection are expensive to
# construct, so they live for the life of the process. Either being None means
# the cache stage is unavailable and every generate request is a miss-
# equivalent (Req 1.6, 1.7, 2.6).
# ---------------------------------------------------------------------------

embedding_model = None            # sentence-transformers model or None
vector_store = None               # Chroma collection or None
cache_log: CacheLog | None = None

# ---------------------------------------------------------------------------
# Power-stage singletons (built ONCE at startup by the lifespan handler below).
# The active Power_Source is detected once and fixed for the process; the
# PowerSampler is the single shared background daemon thread. Either being None
# means the stage is unavailable and every attribution degrades to the honest
# ``unavailable`` marker (Req 1.4, 1.5, 2.2, 10.3).
# ---------------------------------------------------------------------------

power_sampler: PowerSampler | None = None
power_log: PowerLog | None = None


def _active_detectors() -> tuple:
    """Assemble the active detector list for a redaction pass.

    Built-in regex detectors always run. The custom-term detector runs whenever
    the store was constructed at startup. The NER detector runs only when the
    local model loaded successfully (``redaction_state.ner_available``) so the
    graceful-fallback path (Req 3.5) never emits ``person`` redactions before a
    model is available.
    """
    detectors: tuple = tuple(BUILTIN_REGEX_DETECTORS)
    if custom_term_detector is not None:
        detectors += (custom_term_detector,)
    if redaction_state.ner_available and ner_detector is not None:
        detectors += (ner_detector,)
    return detectors


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the redaction stage singletons once at startup (task 11.5).

    Loads the local NER model from its on-device artifact before the first
    generate request is served (Req 3.4). On failure ``load_ner_model`` returns
    ``None`` and we set ``ner_available=False`` so the regex + custom-term
    detectors keep operating and no ``person`` redaction is emitted (Req 3.5).
    The custom-terms store and audit log are also constructed once here. No
    network call is made at any point.
    """
    global ner_detector, custom_terms_store, custom_term_detector, audit_log
    global embedding_model, vector_store, cache_log
    global power_sampler, power_log

    nlp = load_ner_model()
    redaction_state.set_ner_available(nlp is not None)
    ner_detector = NerDetector(nlp)
    if nlp is None:
        logger.warning("NER disabled: local model unavailable; regex + custom-term only")

    custom_terms_store = CustomTermsStore()
    custom_term_detector = CustomTermDetector(custom_terms_store)
    audit_log = AuditLog()

    # ── Semantic cache load (Req 1.5, 1.6, 1.7, 2.1, 2.6) ──
    # Load the local embedding model + open the persistent Chroma collection
    # once. Both loaders return None on ANY failure (never raise, no network);
    # the CacheLog is a plain append-only writer. The stage is usable only when
    # BOTH the model and the store are present — otherwise every generate
    # request degrades to a miss-equivalent and the pipeline runs unchanged.
    embedding_model = semantic_cache.load_embedding_model()
    vector_store = semantic_cache.open_vector_store()
    cache_log = CacheLog()
    cache_available = embedding_model is not None and vector_store is not None
    cache_state.set_cache_available(cache_available)
    if not cache_available:
        logger.warning(
            "Semantic cache disabled: model=%s store=%s (continuing without cache)",
            embedding_model is not None,
            vector_store is not None,
        )

    # ── Power stage detection + sampler start (Req 1.4, 2.2, 7.3) ──
    # Detect the active Power_Source EXACTLY ONCE. detect_power_source runs the
    # powermetrics capability probe ONLY when POWER_TRY_MEASURED=1; otherwise it
    # never invokes ``sudo`` and selects the estimated tier directly. Record the
    # detected identity + measured-tier authorization in power_state (not
    # user-toggleable). Construct the single shared PowerSampler and start it
    # only when the stage is enabled (Req 7.3). Construct the append-only log.
    active_source, measured_authorized = detect_power_source()
    power_state.set_source(active_source.name, measured_authorized=measured_authorized)
    power_sampler = PowerSampler(active_source)
    power_log = PowerLog()
    if power_state.is_enabled():
        power_sampler.start()
    logger.info(
        "Power stage: source=%s measured_authorized=%s enabled=%s",
        active_source.name,
        measured_authorized,
        power_state.is_enabled(),
    )

    # ── OpenTelemetry tracing init (task 6, Req 1.1, 6.3) ──
    # Initialize the single global TracerProvider ONCE, after the other stage
    # singletons. init_tracing() is best-effort (never raises) and returns
    # whether tracing is active; get_tracer() yields a no-op tracer when it is
    # not, so the generate instrumentation below stays unconditional. NO spans
    # are constructed here — this only wires the provider (Req 1.1).
    ok = tracing.init_tracing()
    logger.info("Tracing: %s", "enabled" if ok else "disabled")

    yield

    # ── Shutdown: stop the sampler daemon thread (Req 1.5) ──
    if power_sampler is not None:
        power_sampler.stop()

    # ── OpenTelemetry tracing shutdown (task 6, Req 6.3) ──
    # Best-effort flush + provider shutdown so buffered spans export without
    # hanging shutdown; shutdown_tracing() is guarded and never raises.
    tracing.shutdown_tracing()


app = FastAPI(title="AI FinOps Router", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

ARCHITECT_SYSTEM_PROMPT = """\
You are a dynamic, human-like assistant who helps people EITHER build software \
OR get a knowledge task done. You handle both: \
(1) BUILDING software — a website, CLI tool, API, game, script, bot, library; and \
(2) PERFORMING knowledge tasks — researching a topic, producing structured notes, \
summarising, analysing, comparing, explaining, or writing. \
Never assume it's a web app. \
You MUST read the user's latest message and react to it. \
If they say stop, change the subject, or reject a feature, you must adapt immediately. \
Do not just loop through a hardcoded list of questions.

CRITICAL — never refuse a request as "out of scope." If a user asks you to research, \
take notes, summarise, explain, analyse, or fact-check, treat that as a real task you \
WILL perform (the system will have you produce the actual result) — gather what you \
need (topic, scope, output format) rather than declining. If instead they want a piece \
of software, gather build requirements. Either way, help them; do not say you cannot.

Your goal is to gather just enough requirements — for a build OR for a task to perform \
— through real conversation, not a script.

Rules you must never break:
1. READ THE LATEST USER MESSAGE FIRST. Every reply must directly acknowledge or respond \
to what the user just said. If they push back, refuse, or change direction, honour it immediately.
2. You are ONLY scoping here — you are the intake step, NOT the doer. NEVER produce the \
actual deliverable in this conversation. Do NOT write the poem, the notes, the essay, the \
summary, or the code here. Your job is to gather requirements and then hand off. Once you \
have enough, summarise the request in a short bullet list and end with REQUIREMENTS_COMPLETE \
on its own line — the actual poem/notes/code is produced AFTER that, by the next stage.
3. Your VERY FIRST question must be open-ended: ask what they want to make/build/do, \
without assuming it is an app or website. Let THEM tell you the kind of thing it is.
4. Ask ONE question at a time. Never stack multiple questions in one reply.
5. Do not repeat a question the user has already answered, even partially.
6. Keep scoping SHORT — at most 2-3 questions. For a simple request (e.g. "a poem about \
flowers"), one quick clarifying question is plenty; then summarise and emit \
REQUIREMENTS_COMPLETE. Do not over-interrogate.
7. Tailor follow-up questions to the KIND of request. A CLI tool, a game, a poem, and a \
research task need different questions — do not ask about databases, auth, or deployment \
unless the user is clearly building software that needs them.
8. If the user says "stop", "skip", "I don't care", "doesn't matter", or any equivalent, \
accept it gracefully and move on. Never re-ask a topic they have dismissed.
9. If the user seems frustrated or just wants to get started, acknowledge it and proceed \
with sensible defaults for anything still missing.
10. Be warm, concise, and natural. Match the user's energy and vocabulary.

Dimensions to cover WHEN RELEVANT (through conversation, not interrogation — skip \
any that don't apply). Which set applies depends on whether they want you to BUILD \
software or PERFORM a knowledge task:

If they want to BUILD software (app, tool, script, etc.):
- What the thing is and who/what it's for
- The core features or behaviour it must have
- Language / framework / tech preferences (or sensible defaults)
- Any data, storage, or state it needs
- How it runs or is used (CLI, web, service, library, etc.)
- Any integrations, auth, or deployment needs — only if applicable

If they want you to PERFORM a task (research, notes, summary, analysis, explanation):
- The exact topic or question, and its scope/depth
- The output format they want (structured notes, bullet summary, comparison table, essay)
- Any focus, angle, audience, or constraints
- Do NOT ask about programming language, database, hosting, or deployment — those \
  are irrelevant to a knowledge task.

Once you have enough to work with — explicit answers or accepted defaults — summarise \
what you have in a short bullet list, then end your message with exactly this line by itself:
REQUIREMENTS_COMPLETE

Do not include REQUIREMENTS_COMPLETE until you genuinely have enough. If the user just \
wants to go, state the defaults you will use and include REQUIREMENTS_COMPLETE.\
"""

# ---------------------------------------------------------------------------
# Data Stream Protocol helpers
# 0:<json-string>\n  — text delta
# 2:<json-array>\n   — data annotation (must come before finish frame)
# d:<json-object>\n  — finish (always last)
# ---------------------------------------------------------------------------

def text_delta(chunk: str) -> str:
    return f"0:{json.dumps(chunk)}\n"

def data_annotation(payload: dict) -> str:
    return f"2:{json.dumps([payload])}\n"

def finish_message(finish_reason: str = "stop", usage: dict | None = None) -> str:
    body: dict = {"finishReason": finish_reason}
    if usage:
        body["usage"] = usage
    return f"d:{json.dumps(body)}\n"


def _redaction_annotations(session_id: str, stage_enabled: bool, result) -> list[str]:
    """Build the redaction telemetry frames for the generate phase (task 11.3).

    Returns a list of ``data_annotation`` frames to yield BEFORE ``finish_message``:

      * ``redaction_report`` — session id, ``stageEnabled``, and the session's
        cumulative ``counts`` + ``totalRedactions`` from ``redaction_state``
        (Req 7.1-7.3). When the stage is disabled it reports the empty state
        (``counts={}``, ``totalRedactions=0``) per Req 8.5.
      * ``redaction_benchmark`` — this-request latency, chars redacted, and a
        per-category count map defaulting every built-in category to 0
        (Req 9.2-9.6). If the benchmark payload cannot be built for any reason
        we emit ``redaction_benchmark_unavailable`` and continue (Req 9.7).
    """
    frames: list[str] = []

    # -- redaction_report (cumulative, session-scoped) --
    if stage_enabled:
        report = redaction_state.report_for(session_id)
    else:
        # Disabled stage: empty-state report (Req 8.5). Do NOT read cumulative
        # counts — the stage did not run for this request.
        report = {"counts": {}, "totalRedactions": 0}
    frames.append(data_annotation({
        "event": "redaction_report",
        "sessionId": session_id,
        "stageEnabled": stage_enabled,
        **report,
    }))

    # -- redaction_benchmark (this request), with unavailable fallback --
    try:
        if stage_enabled and result is not None:
            per_category = {cat: 0 for cat in CATEGORIES}
            per_category.update(result.category_counts)
            latency_ms = result.latency_ms
            chars_redacted = result.chars_redacted
        else:
            # Disabled stage: zeroed benchmark (Req 9.6 / 8.5).
            per_category = {cat: 0 for cat in CATEGORIES}
            latency_ms = 0.0
            chars_redacted = 0
        frames.append(data_annotation({
            "event": "redaction_benchmark",
            "sessionId": session_id,
            "stageEnabled": stage_enabled,
            "latencyMs": latency_ms,
            "charsRedacted": chars_redacted,
            "perCategoryCounts": per_category,
        }))
    except Exception as err:  # noqa: BLE001 — benchmark capture must not block
        logger.warning("Redaction benchmark unavailable: %s", err)
        frames.append(data_annotation({
            "event": "redaction_benchmark_unavailable",
            "sessionId": session_id,
        }))

    return frames


# ---------------------------------------------------------------------------
# Cache annotation frame builders (task 12.5, Req 7.1/7.4/7.5/8.5/10.4/10.5/10.7)
#
# Each returns a ``data_annotation(...)`` frame string. Cache-savings fields are
# named DISTINCTLY from the compression-savings fields so the frontend renders
# them in a separate panel. No raw prompt / redacted-prompt text / sensitive
# value is ever placed in these frames — only counts, rates, scalars, and the
# session id.
# ---------------------------------------------------------------------------

def _cache_report_frame(
    session_id: str,
    *,
    stage_enabled: bool,
    hit: bool,
    this_tokens_saved: int = 0,
    this_compute_ms_saved: int = 0,
) -> str:
    """Build the ``cache_report`` frame for this request (Req 7.1/7.5/8.5).

    Merges the CUMULATIVE session accounting from ``cache_state.report_for``
    (hits/misses/decisions/hitRate/tokensSavedFromCache/computeTimeSavedMs) with
    the per-request savings (``tokensSavedThisHit`` / ``computeTimeSavedMsThisHit``)
    so the frontend can show this-request savings distinctly from the running
    totals. When the stage is disabled the report still emits with ``hit=False``
    and the (zero) ``report_for`` snapshot (Req 8.5).
    """
    report = cache_state.report_for(session_id)
    return data_annotation({
        "event": "cache_report",
        "sessionId": session_id,
        "stageEnabled": stage_enabled,
        "hit": hit,
        "tokensSavedThisHit": int(this_tokens_saved),
        "computeTimeSavedMsThisHit": int(this_compute_ms_saved),
        **report,
    })


def _cache_benchmark_frame(
    session_id: str,
    decision: str,
    *,
    lookup_latency_ms: float,
    tokens_saved: int = 0,
    inference_time_saved_ms: int = 0,
) -> str:
    """Build the ``cache_benchmark`` frame for this request (Req 10.4/10.5)."""
    return data_annotation({
        "event": "cache_benchmark",
        "sessionId": session_id,
        "decision": decision,          # "hit" | "miss"
        "lookupLatencyMs": lookup_latency_ms,
        "tokensSaved": int(tokens_saved),
        "inferenceTimeSavedMs": int(inference_time_saved_ms),
    })


def _cache_benchmark_unavailable_frame(session_id: str) -> str:
    """Build the ``cache_benchmark_unavailable`` fallback frame (Req 10.7)."""
    return data_annotation({
        "event": "cache_benchmark_unavailable",
        "sessionId": session_id,
    })


# ---------------------------------------------------------------------------
# Power annotation frame builders (task 8.2, Req 5.2/5.4/5.5/5.6/7.4/9.5/9.7)
#
# Pure helpers mirroring ``_cache_report_frame`` / ``_cache_benchmark_unavailable_frame``.
# Numeric fields carry their unit in the name. UNAVAILABLE / DISABLED numeric
# fields are JSON ``null`` (never ``0``) so the frontend can distinguish a
# measured 0 W from the absence of a reading (Req 5.4/5.6/3.8). The quality flag
# from the attribution is carried through VERBATIM — never upgraded (Req 3.7).
# No raw prompt content or sensitive value ever enters these frames (Req 5.3):
# only scalars, the quality flag, the source name, and the session id.
# ---------------------------------------------------------------------------

def _power_report_frame(
    session_id: str,
    stage_enabled: bool,
    attr: PowerAttribution,
) -> str:
    """Build the per-request ``power_report`` frame (Req 5.2/5.4/5.5/5.6/7.4).

    Covers the measured / estimated / unavailable / disabled variants uniformly:
    the numeric fields are copied straight from ``attr`` (which are ``None`` for
    the unavailable and disabled markers), so an unavailable/disabled report
    emits JSON ``null`` for every numeric figure rather than a fabricated ``0``.
    ``stage_enabled`` False renders the disabled variant on the frontend (Req 7.4).
    """
    return data_annotation({
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
    })


def _power_benchmark_unavailable_frame(session_id: str) -> str:
    """Build the ``power_benchmark_unavailable`` fallback frame (Req 9.7).

    Emitted only when the report frame cannot be built from the attribution;
    telemetry must never block the generate output.
    """
    return data_annotation({
        "event": "power_benchmark_unavailable",
        "sessionId": session_id,
    })


def _attribute_power(
    enabled: bool,
    start: float,
    end: float,
) -> PowerAttribution:
    """Attribute per-request power for one Inference_Window (task 8.3, Req 7.3/4.8/10.3).

    Returns an HONEST marker in the degraded cases and never fabricates a figure:

      * ``not enabled`` → the disabled marker (no sampling ran, no attribution
        math) tagged with the detected source name (Req 7.3, 7.4).
      * ``power_sampler is None`` → the unavailable marker (stage not
        constructed) (Req 10.3).
      * otherwise delegate to ``power_sampler.attribute_window(start, end)``,
        which itself returns the honest ``unavailable`` result when zero readings
        fall in the window (Req 4.8).

    The quality flag it produces is resolved by worst-quality-wins inside
    ``attribute_window`` and is never upgraded here.
    """
    if not enabled:
        return disabled_attribution(power_state.source_name)
    if power_sampler is None:
        return unavailable_attribution(power_state.source_name, max(0.0, end - start))
    return power_sampler.attribute_window(start, end)


def _power_report_and_log(
    session_id: str,
    enabled: bool,
    attr: PowerAttribution,
) -> str:
    """Build the single ``power_report`` frame and best-effort append the log (task 8.5).

    Returns exactly one frame string to yield immediately before ``finish_message``
    on EVERY generate path (hit, miss, PERFORM, BUILD, disabled). Building the
    frame is wrapped so a failure falls back to the ``power_benchmark_unavailable``
    frame and never blocks the generate output (Req 9.7). The append-only log
    entry is best-effort and only written when the stage is enabled and the log
    was constructed (Req 9.1); ``power_log.append`` never raises (Req 9.4).
    """
    try:
        frame = _power_report_frame(session_id, enabled, attr)
    except Exception as err:  # noqa: BLE001 — telemetry must not block output (Req 9.7)
        logger.warning("Power report unavailable: %s", err)
        frame = _power_benchmark_unavailable_frame(session_id)

    if enabled and power_log is not None:
        power_log.append(
            session_id,
            avg_power_watts=attr.avg_power_watts,
            energy_joules=attr.energy_joules,
            source=attr.source,
            quality=attr.quality,
        )

    return frame


def _skipped_compression_frames() -> list[str]:
    """Return the skipped ``compression_stats`` + ``compression_diff`` frames.

    On a cache HIT compression is not run, but the frontend's ``setStats`` /
    ``compressionDiff`` slices still expect these events. We reuse the SAME
    event names (no new frame types) with an added ``skipped: true`` flag and
    zeroed/empty numeric fields so nothing is missing and the diff panel (guarded
    by ``tokens.length > 0``) renders nothing (Req 9.6).
    """
    return [
        data_annotation({
            "event": "compression_stats",
            "skipped": True,
            "originalTokens": 0,
            "compressedTokens": 0,
            "ratio": 0,
            "multiplier": 1,
            "compressedPrompt": "(compression skipped — served from cache)",
        }),
        data_annotation({
            "event": "compression_diff",
            "skipped": True,
            "original": "",
            "tokens": [],
            "multiplier": 1,
        }),
    ]

# ---------------------------------------------------------------------------
# Message normalisation
# ---------------------------------------------------------------------------

def _normalise_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)

def _build_claude_messages(raw: list[dict]) -> list[dict]:
    msgs = [
        {"role": m["role"], "content": _normalise_content(m.get("content", ""))}
        for m in raw
        if m.get("role") in ("user", "assistant")
        and _normalise_content(m.get("content", "")).strip()
    ]
    while msgs and msgs[-1]["role"] != "user":
        msgs.pop()
    return msgs

# ---------------------------------------------------------------------------
# Requirement extraction
# ---------------------------------------------------------------------------

def _clean_requirements_summary(text: str) -> str:
    """
    Turn a chatty, markdown-laden assistant summary into clean, dense
    requirement text suitable for the compressor.

    The elicitation model tends to wrap the actual requirements in
    conversational preamble ("Got you! Here's what I'm locking in:") and
    markdown (**bold**, bullet dashes, headers). Feeding that raw into the
    compressor produced garbled output like "Got You,production-ready So,".
    We normalise it here so compression operates on the substance.
    """
    import re

    # Drop everything up to and including a conversational lead-in that ends at
    # the first ':' introducing the requirement list. Covers phrasings like
    # "here's what I'm locking in:", "let me lock in what we're building:",
    # "to summarise:", "the plan:", etc. We only strip a lead-in that appears
    # near the start (first ~160 chars) so we never chop real requirement text.
    lead_in = re.search(
        r"(?:lock(?:ing)? in|here'?s what|what we'?re building|to summar[iy][sz]e"
        r"|the plan|final(?:is|iz)ing|confirm(?:ing)?)[^:]{0,120}:",
        text[:200],
        re.IGNORECASE,
    )
    if lead_in:
        text = text[lead_in.end():]
    else:
        # Fallback: if the very first sentence is chatty (contains "I've", "I'm",
        # "let me", "got it/you", "great") and a ':' follows soon after, drop up
        # to that first colon.
        m = re.match(
            r".{0,160}?\b(?:I'?ve|I'?m|let me|got (?:it|you)|great|perfect|awesome)\b[^:]{0,120}:",
            text,
            re.IGNORECASE,
        )
        if m:
            text = text[m.end():]

    # Strip markdown emphasis/headers/bullets and list markers.
    text = re.sub(r"[*_`#>]+", " ", text)          # **bold**, __, `code`, #, >
    text = re.sub(r"^\s*[-•]\s*", " ", text, flags=re.MULTILINE)  # bullet dashes

    # Expand a few contractions that carry no signal once compressed.
    text = re.sub(r"\bI'?m\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bHere'?s\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:Got you|Great|Sure|Okay|OK|Alright|Perfect)\b[!,. ]*",
                  "", text, flags=re.IGNORECASE)

    # Collapse whitespace/newlines into single spaces and tidy stray commas.
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*,\s*,+", ", ", text)
    text = re.sub(r"\s+([,.:;])", r"\1", text)
    return text.strip(" ,.;:")


def extract_requirements_summary(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "assistant":
            content = _normalise_content(m.get("content", ""))
            if "REQUIREMENTS_COMPLETE" in content:
                raw = content.replace("REQUIREMENTS_COMPLETE", "").strip()
                return _clean_requirements_summary(raw)
    return " | ".join(
        _normalise_content(m.get("content", ""))
        for m in messages
        if m.get("role") == "user"
    )

def build_generation_prompt(summary: str, compressed: str) -> str:
    return (
        "You are an expert full-stack developer. "
        "Generate complete, production-ready code for the following project.\n\n"
        f"## Requirements\n{summary}\n\n"
        f"## Compressed prompt (cost-optimised)\n{compressed}\n\n"
        "Respond with well-structured, commented code only."
    )


# ---------------------------------------------------------------------------
# Task-type routing: BUILD (make software) vs PERFORM (do a knowledge task)
# ---------------------------------------------------------------------------

_INTENT_SYSTEM = (
    "You are a classifier. Decide whether the user's request is asking to BUILD "
    "a piece of software/tool, or to PERFORM a knowledge task directly (research, "
    "notes, summary, analysis, explanation, comparison, writing).\n"
    "Reply with EXACTLY one word: BUILD or PERFORM. No punctuation, no explanation.\n"
    "Examples:\n"
    "  'a CLI tool to rename files' -> BUILD\n"
    "  'a website for my bakery' -> BUILD\n"
    "  'research recent papers on RAG and give me structured notes' -> PERFORM\n"
    "  'summarise this topic into bullet points' -> PERFORM\n"
    "  'explain how diffusion models work' -> PERFORM\n"
    "  'build me a script that summarises papers' -> BUILD\n"
)

_PERFORM_KEYWORDS = (
    "research", "notes", "summar", "explain", "analy", "compare", "comparison",
    "fact-check", "fact check", "overview", "review the", "write ", "draft",
    "brief", "report on", "study ", "learn about", "tell me about",
)
_BUILD_KEYWORDS = (
    "build", "app", "website", "cli", "tool", "script", "api", "library",
    "game", "bot", "pipeline", "code", "program", "generate a", "make a program",
)

# Strong, unambiguous signals that decide intent WITHOUT asking the (flaky,
# non-deterministic) small local model. Creative/knowledge outputs are PERFORM;
# explicit software artefacts are BUILD.
_STRONG_PERFORM = (
    "poem", "story", "essay", "haiku", "song", "lyrics", "letter", "email",
    "notes", "research", "summary", "summarise", "summarize", "explain",
    "analysis", "analyse", "analyze", "compare", "comparison", "outline",
    "blog post", "article", "review of", "report on", "recipe",
)
_STRONG_BUILD = (
    "web app", "website", "cli tool", "command line", "rest api", "api endpoint",
    "python script", "react app", "next.js", "database", "backend", "frontend",
    "library", "package", "microservice", "chrome extension", "mobile app",
)


async def classify_task_intent(summary: str) -> str:
    """
    Classify the request as 'build' (make software) or 'perform' (produce a
    knowledge/creative output directly).

    Strategy (most reliable first):
      1. Strong keyword match — decides immediately, no model call. This avoids
         the small local model's non-determinism on obvious cases (a "poem" or
         "story" is always PERFORM; a "web app" is always BUILD).
      2. Quick local model classification for genuinely ambiguous requests.
      3. Weak keyword heuristic as a final fallback.
    """
    text = (summary or "").strip()
    if not text:
        return "build"
    low = text.lower()

    # 1. Strong, deterministic keyword signals.
    strong_perform = any(k in low for k in _STRONG_PERFORM)
    strong_build = any(k in low for k in _STRONG_BUILD)
    if strong_perform and not strong_build:
        return "perform"
    if strong_build and not strong_perform:
        return "build"

    # 2. Ask the local model only when strong signals are absent/conflicting.
    try:
        raw, _usage = await invoke_sync(
            system=_INTENT_SYSTEM,
            messages=[{"role": "user", "content": text[:2000]}],
            max_tokens=4,
        )
        answer = raw.strip().upper()
        if "PERFORM" in answer:
            return "perform"
        if "BUILD" in answer:
            return "build"
    except Exception:
        pass

    # 3. Weak keyword fallback.
    perform_hits = sum(1 for k in _PERFORM_KEYWORDS if k in low)
    build_hits = sum(1 for k in _BUILD_KEYWORDS if k in low)
    return "perform" if perform_hits > build_hits else "build"


PERFORM_SYSTEM_PROMPT = (
    "You are a knowledgeable assistant running locally. PERFORM the task the user "
    "describes and produce the actual result directly — do NOT write code, and do "
    "NOT treat the request as a software specification.\n\n"
    "Guidelines:\n"
    "- If asked to research or summarise a topic, produce clear, well-structured "
    "notes using markdown headings and bullet points.\n"
    "- Be accurate and concrete. If you are uncertain or the topic may be beyond "
    "your training, say so briefly rather than inventing facts. You cannot browse "
    "the web, so base answers on what you know.\n"
    "- Match the output format the user asked for (notes, summary, comparison, etc.).\n"
    "- Be thorough but focused; no filler."
)


def build_perform_prompt(summary: str, compressed: str) -> str:
    """Assemble the prompt for a PERFORM task from the compressed requirements."""
    return (
        "Perform the following task and return the finished result directly:\n\n"
        f"{summary}\n\n"
        f"(cost-optimised brief: {compressed})"
    )


# ── Private on-device artifacts: BUILD-path delimiter injection ──────────────
# Feature: private-on-device-artifacts (design §1 "Backend: prompt injection").
#
# When the artifact stage is enabled AND the request routed to BUILD (Req 6.1 —
# NEVER on the PERFORM path), we prepend an Artifact_Delimiter instruction to the
# compressed requirements that flow into ``run_task_router`` so the generated
# code wraps its SINGLE primary renderable artifact in the marker pair
# ``<<<TOKENQUICK_ARTIFACT:lang>>>`` … ``<<<END_ARTIFACT>>>`` (Req 1.1). The
# ``lang`` tag is ``html`` for plain web pages or ``react`` for React components
# (Req 1.4). This is a pure prompt convention — the model is not perfectly
# reliable, which is why the frontend parser + fallback are the load-bearing
# pieces (design §2/§4). When the stage is disabled the compressed prompt passes
# through UNCHANGED, so behavior is identical to today (Req 8.2). Injecting here
# (rather than in redaction/cache/power wiring) keeps those stages untouched
# (Req 6.3).
_ARTIFACT_DELIMITER_INSTRUCTION = (
    "ARTIFACT PREVIEW INSTRUCTION: This project will be shown in a live in-browser "
    "preview inside a sandboxed iframe with a strict Content-Security-Policy "
    "(`default-src 'none'`, `connect-src 'none'`), so the artifact must run with "
    "ZERO network access at runtime. Emit the SINGLE primary renderable artifact "
    "FIRST, as the very first block of your output, wrapped in these exact markers:\n"
    "<<<TOKENQUICK_ARTIFACT:lang>>>\n"
    "...the complete artifact code...\n"
    "<<<END_ARTIFACT>>>\n"
    "Replace `lang` with `html` for a plain web page (vanilla HTML/CSS/JS) or "
    "`react` for a React component. Use these unambiguous markers instead of "
    "Markdown code fences for the primary artifact; any backticks inside the "
    "artifact body are treated as ordinary content. Wrap only ONE artifact this "
    "way; emit any other files normally after the closed artifact block.\n"
    "The wrapped artifact MUST be a SINGLE, SELF-CONTAINED, STANDALONE file (one "
    "complete HTML document). Requirements for it to render under the CSP:\n"
    "- Vanilla HTML5 with an inline `<style>` for all CSS and an inline "
    "`<script>` for all JavaScript — everything in the one document.\n"
    "- NO external CDNs, NO `<link href=...>` or `<script src=...>` pointing at "
    "remote hosts, NO web fonts or images fetched over the network, NO Babel, NO "
    "in-browser JSX/React build step. If you need React-like behaviour, pre-compile "
    "it to plain inline vanilla JavaScript.\n"
    "- Make no `fetch`, `XMLHttpRequest`, WebSocket, or other network calls at "
    "runtime; the CSP's `connect-src 'none'` will block them. Use only in-memory "
    "state and inline assets."
)


def inject_artifact_instruction(compressed: str, *, enabled: bool) -> str:
    """Prepend the Artifact_Delimiter instruction to the compressed BUILD prompt.

    WHEN ``enabled`` (the request-entry snapshot of the artifact-stage toggle)
    the instruction is prepended so it reaches the model via the existing
    ``run_task_router`` → ``decompose`` → subtask prompt-assembly path (Req 1.1,
    6.2). WHEN disabled the ``compressed`` string is returned UNCHANGED so the
    BUILD path behaves exactly as it does today (Req 8.2). This is only ever
    called on the BUILD path — never on PERFORM (Req 6.1).
    """
    if not enabled:
        return compressed
    return f"{_ARTIFACT_DELIMITER_INSTRUCTION}\n\n{compressed}"


# Phrases signalling the user wants to stop scoping and just get the result.
_PROCEED_PHRASES = (
    "just write it", "just do it", "just build it", "just make it", "go ahead",
    "just go", "do it now", "write it now", "make it now", "build it now",
    "generate it", "just generate", "that's enough", "thats enough", "enough questions",
    "stop asking", "no more questions", "just start", "let's go", "lets go", "proceed",
)


def user_wants_to_proceed(messages: list[dict]) -> bool:
    """True if the latest user message signals 'stop scoping, produce the result'."""
    for m in reversed(messages):
        if m.get("role") == "user":
            low = _normalise_content(m.get("content", "")).lower()
            return any(p in low for p in _PROCEED_PHRASES)
    return False


def looks_like_deliverable(text: str) -> bool:
    """
    Heuristic: the elicitation model produced the actual deliverable (a long
    multi-line answer) instead of a short scoping question. Small local models
    sometimes do this despite instructions, so we detect it and route to the
    pipeline rather than letting the answer appear only in the chat.
    """
    t = text.strip()
    if "?" in t and len(t) < 300:
        return False  # short question — normal scoping
    return t.count("\n") >= 4 or len(t) > 600


# ---------------------------------------------------------------------------
# OpenTelemetry generate instrumentation helpers (feature: opentelemetry-tracing)
#
# The generate request splits across a sync/async boundary: redaction + cache +
# compression run SYNCHRONOUSLY in the handler body (before StreamingResponse is
# built), while inference + power run INSIDE the async streaming generator. A
# single ``with safe_span(...)`` cannot wrap the whole thing across the async
# yields without risking the stream, so we open the PARENT span manually with
# ``start_span`` (holding a reference across the boundary) and record each child
# as a SHORT-LIVED span around the synchronous work unit it wraps, nested under
# the parent via ``_otel_trace.use_span(parent, end_on_exit=False)``.
#
# Every span operation here is best-effort: wrapped in try/except (or delegated
# to ``safe_span`` / ``_set_safe_attrs`` which self-guard) so a tracing failure
# NEVER raises into the request or stream path (Req 6.1). These spans are
# ADDITIVE — the existing audit_log / cache_log / power_log writes and every
# data annotation are unchanged (dual-write, Req 7.2/7.3). No value is recomputed
# for a span; every attribute is read from a value already in scope.
# ---------------------------------------------------------------------------


def _start_generate_span(session_id: str):
    """Open the parent ``tokenquick.generate`` span; never raises (Req 6.1).

    Returns the live span (kept across the sync/async boundary and ended in the
    generator's ``finally``), or ``None`` if tracing is unavailable/failed — in
    which case every downstream span op is a guarded no-op.
    """
    try:
        span = tracing.get_tracer().start_span("tokenquick.generate")
        tracing._set_safe_attrs(
            span, {"session.id": session_id, "gen.phase": "generate"}
        )
        return span
    except Exception as err:  # noqa: BLE001 — instrumentation is best-effort.
        logger.warning("Could not start generate span: %s", err)
        return None


def _child_span(parent, name: str, attrs: dict | None = None):
    """Context manager for a child stage span nested under ``parent``.

    Delegates to ``safe_span`` (which self-guards and yields ``None`` on any
    error) while making ``parent`` the current span for the duration so the
    child nests correctly. When ``parent`` is ``None`` the child is still opened
    as a normal (root) span so instrumentation degrades gracefully rather than
    disappearing. Attributes flow through the ``_set_safe_attrs`` allowlist.
    """

    @contextmanager
    def _cm():
        tracer = tracing.get_tracer()
        if parent is None:
            with tracing.safe_span(tracer, name, attrs) as span:
                yield span
            return
        try:
            with _otel_trace.use_span(parent, end_on_exit=False):
                with tracing.safe_span(tracer, name, attrs) as span:
                    yield span
        except Exception as err:  # noqa: BLE001 — never raise into the stream.
            logger.warning("Child span %r failed; continuing: %s", name, err)
            yield None

    return _cm()


def _redaction_span_attrs(stage_enabled: bool, result) -> dict:
    """Scalar redaction attributes mirroring ``_redaction_annotations`` data.

    Sourced from the SAME RedactionResult the telemetry frames use — never raw
    or redacted text. Per-category counts become ``redaction.count.<category>``
    keys (allowlisted by prefix in the choke point).
    """
    attrs: dict = {"stage.enabled": stage_enabled}
    if stage_enabled and result is not None:
        category_counts = result.category_counts or {}
        # This-request total = number of redactions applied this pass, mirroring
        # the benchmark frame's per-category counts (sum == len(redactions)).
        attrs["redaction.total"] = sum(category_counts.values())
        attrs["redaction.chars_redacted"] = result.chars_redacted
        attrs["redaction.latency_ms"] = result.latency_ms
        for category, count in category_counts.items():
            attrs[f"redaction.count.{category}"] = count
    return attrs


def _cache_span_attrs(stage_enabled: bool, decision) -> dict:
    """Scalar cache attributes mirroring the ``cache_log.append`` args.

    Reads the SAME scalars passed to ``cache_log.append`` (decision, scores,
    margin, had_runner_up) — never the raw value. When the stage is disabled or
    no lookup ran, only ``stage.enabled`` is set.
    """
    attrs: dict = {"stage.enabled": stage_enabled}
    if stage_enabled and decision is not None:
        attrs["cache.decision"] = decision.decision
        attrs["cache.top_score"] = decision.top_score
        attrs["cache.runner_up_score"] = decision.runner_up_score
        attrs["cache.margin"] = decision.margin
        attrs["cache.had_runner_up"] = decision.candidate_count >= 2
    return attrs


def _compression_span_attrs(stage_enabled: bool, detail) -> dict:
    """Scalar compression attributes from ``gen_compressed_detail`` stats.

    Never prompt text. On a cache hit compression is skipped → ``detail`` is
    ``None`` and only ``stage.enabled=False`` is set (numeric fields omitted).
    """
    if detail is None:
        return {"stage.enabled": False}
    stats = detail.get("stats", {})
    return {
        "stage.enabled": stage_enabled,
        "compression.original_tokens": stats.get("original_tokens"),
        "compression.compressed_tokens": stats.get("compressed_tokens"),
        "compression.ratio": stats.get("ratio"),
    }


def _power_span_attrs(attr) -> dict:
    """Scalar power attributes mirroring the ``power_log`` fields.

    Always sets ``power.source`` and ``power.quality``; the numeric figures
    (``power.avg_watts`` / ``power.energy_joules``) are passed straight from the
    attribution — which are ``None`` for the disabled/unavailable markers — and
    the ``_set_safe_attrs`` choke point DROPS ``None`` (null-not-zero by absence,
    never coerced to ``0``). Values are the SAME scalars written to power_log.
    """
    return {
        "power.source": attr.source,
        "power.quality": attr.quality,
        "power.avg_watts": attr.avg_power_watts,
        "power.energy_joules": attr.energy_joules,
    }


# ---------------------------------------------------------------------------
# /api/chat  — elicitation passthrough
# ---------------------------------------------------------------------------

@app.post("/api/chat")
async def chat(request: Request) -> StreamingResponse:
    body = await request.json()
    messages: list[dict] = body.get("messages", [])
    phase: str = body.get("phase", "elicit")
    session_id: str = (body.get("sessionId") or "").strip()

    # ── Pre-stream redaction for the generate phase (tasks 11.1, 11.2, 11.4) ──
    # The redaction stage and sessionId validation run HERE, in the handler
    # body BEFORE the StreamingResponse is created, so a failure becomes a real
    # HTTP 400/500 (fail-closed: nothing streamed, the raw prompt is never
    # passed downstream). The elicit phase does not redact and is unaffected.
    #
    # These are computed up front and closed over by the streaming generator.
    gen_summary: str = ""
    gen_redacted: str = ""
    gen_compressed_detail: dict | None = None
    gen_stage_enabled: bool = False
    gen_redaction_result = None  # RedactionResult when the stage ran

    # ── Semantic-cache pre-stream state (tasks 12.2/12.3/12.4) ──
    # Computed in the handler body after redaction is OK and closed over by the
    # streaming generator. The cache is consulted ONLY after redaction succeeds
    # (redaction fail-closes with HTTP 500 before we ever get here).
    gen_cache_enabled: bool = False
    gen_cache_hit: bool = False
    gen_cache_decision = None       # semantic_cache.CacheDecision or None
    gen_cache_entry: dict | None = None
    gen_query_embedding = None

    # ── Power-stage pre-stream snapshot (task 8.3) ──
    # Snapshot the toggle at request entry alongside gen_cache_enabled; an
    # in-flight request keeps this snapshot even if the toggle flips mid-stream
    # (Req 7.2). The existing missing-sessionId → HTTP 400 guard already runs
    # before any attribution (Req 5.7).
    gen_power_enabled: bool = False

    # ── OpenTelemetry parent span (feature: opentelemetry-tracing, task 7.1) ──
    # The parent tokenquick.generate span is opened AFTER the sessionId→400 guard
    # and kept across the sync/async boundary: the synchronous stage children
    # (redaction/cache/compression) are recorded here, the inference + power
    # children inside the generator, and the parent is ended in the generator's
    # ``finally``. ``None`` when tracing is unavailable → every span op no-ops.
    gen_span = None

    if phase == "generate":
        # sessionId is REQUIRED for generate — missing/empty is a CLIENT ERROR
        # (design: never invent a per-request uuid). Reject with HTTP 400.
        if not session_id:
            raise HTTPException(status_code=400, detail="missing sessionId")

        # Open the parent span now that sessionId is validated (Req 3.1, 3.2).
        gen_span = _start_generate_span(session_id)

        gen_summary = extract_requirements_summary(messages)

        # Snapshot the toggle at request entry; in-flight requests keep this
        # state even if the toggle flips mid-stream (Req 8.3).
        gen_stage_enabled = redaction_state.is_enabled()

        # Snapshot the power-stage toggle at request entry too (task 8.3, Req 7.2).
        gen_power_enabled = power_state.is_enabled()

        # Snapshot the artifact-stage toggle at request entry too (Req 8.3); the
        # BUILD path uses it to decide whether to inject the Artifact_Delimiter
        # instruction. In-flight generations keep this snapshot even if the
        # toggle flips mid-stream.
        gen_artifact_enabled = artifact_state.is_enabled()

        # The synchronous pre-stream stage work (redaction → cache → compression)
        # is wrapped so that if it aborts with an HTTP error the parent span is
        # marked ERROR and ended here — the streaming generator (which would
        # otherwise end it in its ``finally``) is never created on this path.
        # The exception event carries NO raw content (Req 3.3, 5.1).
        try:
            if gen_stage_enabled:
                # Live-reload custom terms at request entry (Req 4.6) — cheap mtime
                # check; the store/model are NOT rebuilt per request.
                if custom_terms_store is not None:
                    custom_terms_store.maybe_reload()

                # Run redaction on the summary BEFORE compression, wrapped so ANY
                # failure fails closed with HTTP 500 (Req 1.7). The raw summary is
                # never passed to compression, the model, or any annotation.
                try:
                    result = redact(
                        gen_summary,
                        session_id,
                        detectors=_active_detectors(),
                        audit=audit_log,
                        state=redaction_state,
                    )
                except Exception as err:  # noqa: BLE001 — fail closed pre-stream
                    logger.warning("Redaction raised; aborting generate request: %s", err)
                    raise HTTPException(status_code=500, detail="redaction failed") from err

                if not result.ok:
                    # Detector error inside redact(): abort, nothing streamed.
                    raise HTTPException(status_code=500, detail="redaction failed")

                gen_redaction_result = result
                gen_redacted = result.redacted_text
            else:
                # Disabled stage: pass the summary through byte-for-byte unchanged,
                # no scan / redaction / audit append (Req 8.2).
                gen_redacted = gen_summary

            # ── redaction child span (task 7.2) — additive; the audit_log write
            # above is unchanged. Scalars only, never raw/redacted text.
            with _child_span(
                gen_span,
                "tokenquick.redaction",
                _redaction_span_attrs(gen_stage_enabled, gen_redaction_result),
            ):
                pass

            # ── Semantic cache lookup (task 12.2, Req 4.1/6.6/8.1/8.2/9.1-9.3) ──
            # Consulted AFTER redaction is OK for BOTH the enabled and disabled
            # paths — the cache always operates on ``gen_redacted``. The stage runs
            # only when the toggle is on AND the backends loaded (Req 8.1/8.2).
            gen_cache_enabled = cache_state.is_enabled() and cache_state.cache_available
            with _child_span(gen_span, "tokenquick.cache_lookup") as _cache_span:
                if gen_cache_enabled:
                    gen_query_embedding = semantic_cache.embed(embedding_model, gen_redacted)
                    gen_cache_decision, gen_cache_entry = semantic_cache.lookup(
                        vector_store, gen_query_embedding
                    )
                    # Append one append-only cache-decision log entry (Req 4.1); no raw
                    # value — only scores + margin + scalar had_runner_up.
                    cache_log.append(
                        session_id,
                        gen_cache_decision.decision,
                        top_score=gen_cache_decision.top_score,
                        runner_up_score=gen_cache_decision.runner_up_score,
                        margin=gen_cache_decision.margin,
                        had_runner_up=(gen_cache_decision.candidate_count >= 2),
                    )
                    gen_cache_hit = gen_cache_decision.decision == "hit"
                # Mirror the SAME scalars passed to cache_log.append onto the span
                # (additive dual-write); disabled → only stage.enabled=false.
                tracing._set_safe_attrs(
                    _cache_span,
                    _cache_span_attrs(gen_cache_enabled, gen_cache_decision),
                )

            # Compression runs ONLY on a miss (or when the cache is disabled). On a
            # HIT we skip compression + placeholder verification entirely and leave
            # ``gen_compressed_detail`` as None (Req 6.6/9.3).
            if not gen_cache_hit:
                with _child_span(gen_span, "tokenquick.compression") as _comp_span:
                    gen_compressed_detail = compress_prompt_detailed(gen_redacted)

                    # Verify placeholders survived compression only when redaction ran
                    # (there is nothing to verify on the disabled pass). Corruption is a
                    # redaction failure → abort with HTTP 500 (Req 11.5, 1.7).
                    if gen_stage_enabled and not verify_placeholders(
                        gen_redacted, gen_compressed_detail["compressed"]
                    ):
                        logger.warning("Placeholder corruption detected; aborting generate request")
                        raise HTTPException(status_code=500, detail="redaction failed")
                    # Scalar compression metrics only — never prompt text.
                    tracing._set_safe_attrs(
                        _comp_span,
                        _compression_span_attrs(True, gen_compressed_detail),
                    )
        except Exception:
            # Pre-stream abort: the stream generator will not run, so mark the
            # parent span ERROR + end it here. No raw content on the event.
            try:
                if gen_span is not None:
                    gen_span.record_exception(Exception("generate pre-stream failed"))
                    gen_span.set_status(Status(StatusCode.ERROR))
                    gen_span.end()
            except Exception:  # noqa: BLE001 — tracing never masks the real error.
                pass
            raise

    async def stream() -> AsyncGenerator[str, None]:

        if phase == "elicit":
            claude_messages = _build_claude_messages(messages)
            if not claude_messages:
                claude_messages = [{"role": "user", "content": "Hello"}]

            accumulated = ""
            token_count = 0
            async for token in stream_chat(
                system=ARCHITECT_SYSTEM_PROMPT,
                messages=claude_messages,
                max_tokens=512,
            ):
                accumulated += token
                token_count += 1
                yield text_delta(token)

            # Decide whether scoping is complete. Primary signal is the model
            # emitting REQUIREMENTS_COMPLETE, but small local models are
            # unreliable at that — so we ALSO complete when the user clearly
            # wants to proceed, or when the model already produced a full
            # deliverable instead of a scoping question. Either way we hand off
            # to the pipeline so the result goes through compression + reporting.
            complete = (
                "REQUIREMENTS_COMPLETE" in accumulated
                or user_wants_to_proceed(messages)
                or looks_like_deliverable(accumulated)
            )
            if complete:
                yield data_annotation({"event": "phase_complete", "nextPhase": "generate"})

            yield finish_message("stop", {"completionTokens": token_count})

        elif phase == "generate":
            # The redaction stage + sessionId validation already ran in the
            # handler body before this generator started (fail-closed). Use the
            # REDACTED text everywhere downstream — the raw summary never leaves
            # the handler. ``summary`` here is the redacted summary, so every
            # annotation (compression_stats, compression_diff) is computed from
            # redacted text and can contain no raw sensitive value (Req 1.5).
            summary = gen_redacted

            # ── OpenTelemetry parent-span lifetime (feature: opentelemetry-tracing)
            # The parent tokenquick.generate span was opened pre-stream (with its
            # redaction/cache/compression children). Here it wraps the inference +
            # power children and is ALWAYS ended in the ``finally`` so it spans the
            # whole request across the async yields. On an in-stream exception the
            # parent is marked ERROR (no raw content — Req 3.3) and re-raised. All
            # span ops are guarded so tracing NEVER breaks the stream (Req 6.1).
            try:
              try:
                # ── HIT PATH (task 12.3, Req 6.5/9.3-9.6/10.5) ───────────────────
                # A confident, unambiguous cache hit short-circuits compression,
                # PERFORM/BUILD inference, and any store. We reconstruct exactly the
                # frames the frontend expects (using stored metadata) so every panel
                # stays coherent, then stream the cached result text.
                if gen_cache_hit:
                    md = gen_cache_entry["metadata"]
                    tokens_saved = int(md.get("real_total_tokens", 0))
                    time_saved = int(md.get("inference_time_ms", 0))
                    task_mode_val = md.get("task_mode", "perform")

                    # Parent span: task mode is now known (Req 3.2).
                    tracing._set_safe_attrs(gen_span, {"gen.task_mode": task_mode_val})

                    cache_state.record_decision(
                        session_id,
                        hit=True,
                        tokens_saved=tokens_saved,
                        compute_ms_saved=time_saved,
                    )

                    # Skipped compression frames (reuse existing event names).
                    for frame in _skipped_compression_frames():
                        yield frame

                    # Redaction still ran this request — emit its telemetry.
                    for frame in _redaction_annotations(
                        session_id, gen_stage_enabled, gen_redaction_result
                    ):
                        yield frame

                    # Cache report + benchmark (hit), then task_mode from metadata.
                    yield _cache_report_frame(
                        session_id,
                        stage_enabled=True,
                        hit=True,
                        this_tokens_saved=tokens_saved,
                        this_compute_ms_saved=time_saved,
                    )
                    yield _cache_benchmark_frame(
                        session_id,
                        "hit",
                        lookup_latency_ms=gen_cache_decision.latency_ms,
                        tokens_saved=tokens_saved,
                        inference_time_saved_ms=time_saved,
                    )
                    yield data_annotation({"event": "task_mode", "mode": task_mode_val})

                    # Reconstruct a real_usage annotation from stored token counts so
                    # the token panel stays populated (design: HIT-path real_usage).
                    yield data_annotation({
                        "event": "real_usage",
                        "realInputTokens": int(md.get("real_input_tokens", 0)),
                        "realOutputTokens": int(md.get("real_output_tokens", 0)),
                        "realTotalTokens": tokens_saved,
                        "realCostUnits": tokens_saved,
                        "realInputCostUsd": 0.0,
                        "realOutputCostUsd": 0.0,
                        "realTotalCostUsd": 0.0,
                        "perSubtask": [],
                    })

                    # Stream the cached result text via 0: deltas in 16-char chunks.
                    result_text = gen_cache_entry["document"]
                    for i in range(0, len(result_text), 16):
                        yield text_delta(result_text[i:i + 16])
                        await asyncio.sleep(0)

                    # ── Power report (task 8.5, Req 4.8/5.1/8.4) ──
                    # No inference was performed on a cache hit, so per-request power
                    # is HONESTLY unavailable: construct the marker directly (sample
                    # count 0, figures None) rather than inventing a number. When the
                    # stage is disabled the disabled marker is emitted instead.
                    if gen_power_enabled:
                        hit_attr = unavailable_attribution(power_state.source_name)
                    else:
                        hit_attr = disabled_attribution(power_state.source_name)
                    # Power child span (additive; the power_log write is unchanged).
                    with _child_span(gen_span, "tokenquick.power") as _pspan:
                        tracing._set_safe_attrs(
                            _pspan, _power_span_attrs(hit_attr)
                        )
                    yield _power_report_and_log(session_id, gen_power_enabled, hit_attr)

                    yield finish_message("stop")
                    return

                # ── MISS PATH (compression + routing, unchanged pipeline) ────────
                detail = gen_compressed_detail
                compressed = detail["compressed"]
                stats = detail["stats"]

                # Emit compression stats first (now includes the 2×–5× multiplier)
                yield data_annotation({
                    "event": "compression_stats",
                    "originalTokens": stats["original_tokens"],
                    "compressedTokens": stats["compressed_tokens"],
                    "ratio": stats["ratio"],
                    "multiplier": stats["multiplier"],
                    "compressedPrompt": compressed,
                })

                # Emit the token-level before/after diff so the UI can show the
                # original text with discarded tokens visibly struck through — the
                # "watch your language get reorganized" moment that sets this apart
                # from invisible backend routers. ``original`` is the REDACTED
                # summary, never the raw one (Req 1.5).
                yield data_annotation({
                    "event": "compression_diff",
                    "original": summary,
                    "tokens": detail["tokens"],
                    "multiplier": stats["multiplier"],
                })

                # ── Redaction telemetry: emit BEFORE finish_message (task 11.3) ──
                for frame in _redaction_annotations(
                    session_id,
                    gen_stage_enabled,
                    gen_redaction_result,
                ):
                    yield frame

                # ── MISS-path cache annotations (task 12.4, Req 8.5/10.4/10.5/10.7)
                # A REAL miss (cache enabled + available) records the decision and
                # emits cache_report(hit:false) + cache_benchmark(miss). When the
                # cache is disabled/unavailable we emit only the disabled cache_report
                # variant (Req 8.5) — no record_decision, no benchmark.
                if gen_cache_enabled:
                    cache_state.record_decision(session_id, hit=False)
                    yield _cache_report_frame(session_id, stage_enabled=True, hit=False)
                    try:
                        yield _cache_benchmark_frame(
                            session_id,
                            "miss",
                            lookup_latency_ms=(
                                gen_cache_decision.latency_ms
                                if gen_cache_decision is not None
                                else 0.0
                            ),
                            tokens_saved=0,
                            inference_time_saved_ms=0,
                        )
                    except Exception as err:  # noqa: BLE001 — benchmark must not block
                        logger.warning("Cache benchmark unavailable: %s", err)
                        yield _cache_benchmark_unavailable_frame(session_id)
                else:
                    # Disabled / unavailable stage — report the disabled variant.
                    yield _cache_report_frame(session_id, stage_enabled=False, hit=False)

                # ── Task-type routing: PERFORM the task or BUILD a tool ──────────
                intent = await classify_task_intent(summary)
                # Parent span: task mode is now resolved (Req 3.2).
                tracing._set_safe_attrs(gen_span, {"gen.task_mode": intent})
                yield data_annotation({"event": "task_mode", "mode": intent})

                if intent == "perform":
                    # Do the knowledge task directly — no decomposition, no code
                    # pipeline. Use invoke_sync so we get REAL local token counts,
                    # then stream the result out in chunks for a live feel.
                    perform_prompt = build_perform_prompt(summary, compressed)
                    t0 = time.perf_counter()
                    # ── Bracket the Inference_Window around invoke_sync (task 8.4,
                    # Req 4.1/4.6). Wall-clock time.time() matches PowerReading.timestamp.
                    window_start = time.time()
                    # ── inference child span (task 7.3) wraps invoke_sync; scalar
                    # token/timing metadata only, never prompt/generated text.
                    with _child_span(gen_span, "tokenquick.inference") as _ispan:
                        answer, usage = await invoke_sync(
                            system=PERFORM_SYSTEM_PROMPT,
                            # Raised to 4096 so a standalone interactive HTML tool
                            # generated on the PERFORM path completes instead of
                            # being cut off mid-file (Fix: token exhaustion). The
                            # BUILD path already uses SUBTASK_MAX_TOKENS (8192).
                            max_tokens=4096,
                        )
                        window_end = time.time()
                        infer_ms = int((time.perf_counter() - t0) * 1000)
                        real_in = usage.get("input_tokens", 0)
                        real_out = usage.get("output_tokens", 0)
                        tracing._set_safe_attrs(_ispan, {
                            "inference.mode": "perform",
                            "inference.real_input_tokens": real_in,
                            "inference.real_output_tokens": real_out,
                            "inference.time_ms": infer_ms,
                        })
                    power_attr = _attribute_power(gen_power_enabled, window_start, window_end)
                    result_text = answer
                    for i in range(0, len(answer), 16):
                        yield text_delta(answer[i:i + 16])
                        await asyncio.sleep(0)

                    # Emit real local token usage so the perform path shows the same
                    # ground-truth token panel as the build path.
                    yield data_annotation({
                        "event": "real_usage",
                        "realInputTokens": real_in,
                        "realOutputTokens": real_out,
                        "realTotalTokens": real_in + real_out,
                        "realCostUnits": real_in + real_out,
                        "realInputCostUsd": 0.0,
                        "realOutputCostUsd": 0.0,
                        "realTotalCostUsd": 0.0,
                        "perSubtask": [],
                    })

                    # ── Store-after-produce (task 12.4, Req 6.1-6.3/6.7/6.8) ──
                    # Store the produced result on a real miss with a non-empty
                    # result. store() never raises (returns False on failure).
                    if gen_cache_enabled and result_text.strip():
                        metadata = {
                            "task_mode": "perform",
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "real_input_tokens": real_in,
                            "real_output_tokens": real_out,
                            "real_total_tokens": real_in + real_out,
                            "inference_time_ms": infer_ms,
                            "result_char_len": len(result_text),
                        }
                        semantic_cache.store(
                            vector_store, gen_query_embedding, result_text, metadata
                        )

                    # ── Power child span + report (task 7.3 / 8.5, Req 5.1) ──
                    with _child_span(gen_span, "tokenquick.power") as _pspan:
                        tracing._set_safe_attrs(_pspan, _power_span_attrs(power_attr))
                    yield _power_report_and_log(session_id, gen_power_enabled, power_attr)

                    yield finish_message("stop")
                    return

                # ── BUILD path (existing code pipeline) ──────────────────────────
                # Buffer for router events collected before we can yield them
                pending_events: list[tuple] = []
                done = False
                result_text = ""

                # run_task_router calls emit() for every status update.
                # We collect them all, then yield everything after the router finishes.
                # This avoids the asyncio.create_task-inside-generator problem.
                async def collect_emit(event: dict):
                    pending_events.append(("annotation", event))

                # ── Private on-device artifacts (feature: private-on-device-artifacts) ─
                # BUILD path ONLY (Req 6.1): when the artifact stage is enabled, prepend
                # the Artifact_Delimiter instruction so the generated code wraps its
                # single primary renderable artifact in the marker pair (Req 1.1, 6.2).
                # When disabled, ``build_compressed`` is ``compressed`` unchanged
                # (Req 8.2). This never touches the PERFORM path or the redaction/cache/
                # power wiring (Req 6.1, 6.3).
                build_compressed = inject_artifact_instruction(
                    compressed, enabled=gen_artifact_enabled
                )

                t0 = time.perf_counter()
                # ── Bracket the Inference_Window around the full router span (task
                # 8.4, Req 4.1/4.6). Wall-clock time.time() matches PowerReading.timestamp.
                window_start = time.time()
                # ── inference child span (task 7.3) wraps run_task_router; scalar
                # token/timing metadata only, never code/prompt text.
                with _child_span(gen_span, "tokenquick.inference") as _ispan:
                    result_text = await run_task_router(
                        build_compressed,
                        collect_emit,
                        uncompressed_input_tokens=stats["original_tokens"],
                    )
                    window_end = time.time()
                    infer_ms = int((time.perf_counter() - t0) * 1000)
                    # Read the router's buffered real_usage for the span's scalars
                    # (the same values reused for store-after-produce below).
                    _b_in = _b_out = _b_total = 0
                    for _kind, _payload in pending_events:
                        if _kind == "annotation" and _payload.get("event") == "real_usage":
                            _b_in = int(_payload.get("realInputTokens", 0))
                            _b_out = int(_payload.get("realOutputTokens", 0))
                            _b_total = int(_payload.get("realTotalTokens", 0))
                            break
                    tracing._set_safe_attrs(_ispan, {
                        "inference.mode": "build",
                        "inference.real_input_tokens": _b_in,
                        "inference.real_output_tokens": _b_out,
                        "inference.time_ms": infer_ms,
                    })
                power_attr = _attribute_power(gen_power_enabled, window_start, window_end)

                # Now yield all collected annotations
                for kind, payload in pending_events:
                    if kind == "annotation":
                        yield data_annotation(payload)

                # Stream the assembled code
                for i in range(0, len(result_text), 16):
                    yield text_delta(result_text[i:i + 16])
                    await asyncio.sleep(0)

                # ── Store-after-produce (task 12.4, Req 6.1-6.3/6.7/6.8) ──
                # Read the real token counts from the router's buffered real_usage
                # event; store on a real miss with a non-empty result.
                if gen_cache_enabled and result_text.strip():
                    real_in = real_out = real_total = 0
                    for kind, payload in pending_events:
                        if kind == "annotation" and payload.get("event") == "real_usage":
                            real_in = int(payload.get("realInputTokens", 0))
                            real_out = int(payload.get("realOutputTokens", 0))
                            real_total = int(payload.get("realTotalTokens", 0))
                            break
                    metadata = {
                        "task_mode": "build",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "real_input_tokens": real_in,
                        "real_output_tokens": real_out,
                        "real_total_tokens": real_total or (real_in + real_out),
                        "inference_time_ms": infer_ms,
                        "result_char_len": len(result_text),
                    }
                    semantic_cache.store(
                        vector_store, gen_query_embedding, result_text, metadata
                    )

                # ── Power child span + report (task 7.3 / 8.5, Req 5.1) ──
                with _child_span(gen_span, "tokenquick.power") as _pspan:
                    tracing._set_safe_attrs(_pspan, _power_span_attrs(power_attr))
                yield _power_report_and_log(session_id, gen_power_enabled, power_attr)

                yield finish_message("stop")
              except Exception:
                # In-stream failure: mark the parent span ERROR without raw
                # content (Req 3.3), then re-raise so the request fails as before.
                try:
                    if gen_span is not None:
                        gen_span.record_exception(Exception("generate stream failed"))
                        gen_span.set_status(Status(StatusCode.ERROR))
                except Exception:  # noqa: BLE001 — tracing never masks the real error.
                    pass
                raise
            finally:
                # ALWAYS end the parent span so it closes across the async yields,
                # on every path (hit/perform/build/error). Guarded — never raises.
                try:
                    if gen_span is not None:
                        gen_span.end()
                except Exception:  # noqa: BLE001
                    pass

        else:
            raise HTTPException(status_code=400, detail=f"Unknown phase: {phase!r}")

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "x-vercel-ai-data-stream": "v1",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )

# ---------------------------------------------------------------------------
# Redaction management endpoints
#
# Custom-terms endpoints (task 12.1, Req 5) and toggle endpoints (task 12.2,
# Req 8). These are small, typed endpoints so we use Pydantic request models for
# clean 422 validation, in contrast to /api/chat's raw ``await request.json()``.
# They wire to the singletons initialized in the lifespan handler — they do NOT
# reimplement any store/state logic.
#
# The custom-terms endpoints guard for the not-yet-initialized store (endpoint
# hit before startup completed) by returning HTTP 503 rather than an
# AttributeError. The toggle endpoints use the always-available module-level
# ``redaction_state`` singleton, so they need no such guard.
# ---------------------------------------------------------------------------

from pydantic import BaseModel


class TermBody(BaseModel):
    term: str


class ToggleBody(BaseModel):
    enabled: bool


def _require_terms_store() -> CustomTermsStore:
    """Return the custom-terms store or raise 503 if startup hasn't run yet."""
    if custom_terms_store is None:
        raise HTTPException(
            status_code=503,
            detail="custom-terms store not initialized",
        )
    return custom_terms_store


@app.get("/api/redaction/terms")
async def get_terms():
    """Return the current custom-term list (empty when none) (Req 5.1)."""
    store = _require_terms_store()
    return {"terms": store.terms()}


@app.post("/api/redaction/terms")
async def add_term(body: TermBody):
    """Add a custom term; return the updated list (Req 5.2, 5.3, 5.5, 5.7).

    - store.add trims whitespace and returns the updated list on success.
    - A case-insensitive duplicate is a no-op returning the current list.
    - Empty-after-trim or >256 chars raises ValueError -> HTTP 400.
    - A persistence failure (non-ValueError, e.g. OSError) -> HTTP 500 with the
      in-memory list retained by the store (rollback is handled inside add()).
    """
    store = _require_terms_store()
    try:
        terms = store.add(body.term)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    except Exception as err:  # noqa: BLE001 — persistence failure -> 500 (Req 5.7)
        raise HTTPException(status_code=500, detail="not persisted") from err
    return {"terms": terms}


@app.delete("/api/redaction/terms")
async def delete_term(body: TermBody):
    """Remove a custom term case-insensitively (Req 5.4, 5.6, 5.7).

    - Present term -> {"terms": [...], "removed": true}.
    - Absent term -> {"terms": [...unchanged], "removed": false}.
    - Persistence failure -> HTTP 500 with the in-memory list retained.
    """
    store = _require_terms_store()
    try:
        terms, removed = store.remove(body.term)
    except Exception as err:  # noqa: BLE001 — persistence failure -> 500 (Req 5.7)
        raise HTTPException(status_code=500, detail="not persisted") from err
    return {"terms": terms, "removed": removed}


@app.get("/api/redaction/toggle")
async def get_toggle():
    """Return the current redaction-stage toggle state (Req 8.4)."""
    return {"enabled": redaction_state.is_enabled()}


@app.post("/api/redaction/toggle")
async def set_toggle(body: ToggleBody):
    """Set the redaction-stage toggle (Req 8.3).

    The new state applies to generate requests that BEGIN after the change;
    in-flight requests keep their prior state because the generate handler
    snapshots ``redaction_state.is_enabled()`` at request entry, so setting the
    flag here is all that's required.
    """
    redaction_state.set_enabled(body.enabled)
    return {"enabled": redaction_state.is_enabled()}


# ---------------------------------------------------------------------------
# Cache toggle endpoints (task 13.1, Req 8.3/8.4/8.6)
#
# Mirror the redaction toggle endpoints, reusing the existing ``ToggleBody``.
# The new state applies to generate requests that BEGIN after the change;
# in-flight requests keep the snapshot taken at request entry (the generate
# handler snapshots ``cache_state.is_enabled()`` when computing
# ``gen_cache_enabled``). Availability is set by the lifespan loader and is not
# user-toggleable.
# ---------------------------------------------------------------------------

@app.get("/api/cache/toggle")
async def get_cache_toggle():
    """Return the current cache-stage toggle state (Req 8.4)."""
    return {"enabled": cache_state.is_enabled()}


@app.post("/api/cache/toggle")
async def set_cache_toggle(body: ToggleBody):
    """Set the cache-stage toggle (Req 8.3)."""
    cache_state.set_enabled(body.enabled)
    return {"enabled": cache_state.is_enabled()}


# ---------------------------------------------------------------------------
# Power toggle + live-reading endpoints (task 10, Req 7.1/7.2/7.3, 8.1-8.3, 8.5)
#
# Mirror the redaction/cache toggle endpoints, reusing the existing
# ``ToggleBody``. The new toggle state applies to generate requests that BEGIN
# after the change; in-flight requests keep the ``gen_power_enabled`` snapshot
# taken at request entry. The detected source identity is set once by the
# lifespan loader and is not user-toggleable.
# ---------------------------------------------------------------------------

@app.get("/api/power/toggle")
async def get_power_toggle():
    """Return the current power-stage toggle state (Req 7.1)."""
    return {"enabled": power_state.is_enabled()}


@app.post("/api/power/toggle")
async def set_power_toggle(body: ToggleBody):
    """Set the power-stage toggle (Req 7.2, 7.3).

    Start or stop the shared sampler so a disabled stage performs NO sampling
    (Req 7.3); ``start``/``stop`` are idempotent. The new state applies to
    generate requests that BEGIN after the change; in-flight requests keep the
    ``gen_power_enabled`` snapshot taken at request entry.
    """
    power_state.set_enabled(body.enabled)
    if power_sampler is not None:
        if body.enabled:
            power_sampler.start()
        else:
            power_sampler.stop()
    return {"enabled": power_state.is_enabled()}


# ---------------------------------------------------------------------------
# Artifact toggle endpoints (feature: private-on-device-artifacts, Req 8.1/8.3/8.4)
#
# Mirror the redaction/cache/power toggle endpoints, reusing the existing
# ``ToggleBody``. The new state applies to generate requests that BEGIN after
# the change; in-flight requests keep the ``gen_artifact_enabled`` snapshot taken
# at request entry (Req 8.3). Unlike the power stage this is purely a prompt-
# shaping + UI flag — there is NO sampler to start/stop, so there is no
# start/stop logic here. Toggling it never alters redaction, cache, or power
# state (Req 8.4).
# ---------------------------------------------------------------------------

@app.get("/api/artifacts/toggle")
async def get_artifacts_toggle():
    """Return the current artifact-stage toggle state (Req 8.1)."""
    return {"enabled": artifact_state.is_enabled()}


@app.post("/api/artifacts/toggle")
async def set_artifacts_toggle(body: ToggleBody):
    """Set the artifact-stage toggle (Req 8.3, 8.4).

    Purely a flag: the new state applies to generations that BEGIN after the
    change; in-flight generations keep the ``gen_artifact_enabled`` snapshot
    taken at request entry. No sampler/start-stop side effects (unlike power).
    """
    artifact_state.set_enabled(body.enabled)
    return {"enabled": artifact_state.is_enabled()}


@app.get("/api/power/current")
async def get_power_current():
    """Read-only live Current_Power_Reading (Req 8.1-8.3, 8.5).

    Disabled stage or no sampler → the ``unavailable`` shape with null
    components (Req 8.5). Otherwise return the current reading's quality,
    source, and cpu/gpu/package watts, preserving null-not-zero (a component
    the source omits stays ``None`` and is never coerced to ``0``). Reads only a
    lock-guarded snapshot of the single current reading; never blocks the
    sampler.
    """
    if not power_state.is_enabled() or power_sampler is None:
        return {
            "quality": "unavailable",
            "source": power_state.source_name,
            "cpuWatts": None,
            "gpuWatts": None,
            "packageWatts": None,
        }
    reading = power_sampler.current_reading
    if reading is None:
        return {
            "quality": "unavailable",
            "source": power_state.source_name,
            "cpuWatts": None,
            "gpuWatts": None,
            "packageWatts": None,
        }
    return {
        "quality": reading.quality,
        "source": reading.source,
        "cpuWatts": reading.cpu_watts,
        "gpuWatts": reading.gpu_watts,
        "packageWatts": reading.package_watts,
    }


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    if PROVIDER != "bedrock":
        return {
            "status": "ok",
            "provider": "local",
            "lightModel": os.getenv("ROUTER_MODEL_LIGHT", "llama3.2:3b"),
            "heavyModel": os.getenv("ROUTER_MODEL_HEAVY", "qwen2.5-coder:7b"),
            "ollamaHost": os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        }
    return {
        "status": "ok",
        "provider": "bedrock",
        "model": os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    }
