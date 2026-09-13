"""
task_router.py — Task Decomposer, Model Router, and Parallel Executor

Pipeline:
  1. DECOMPOSE  — Ask a cheap model to split the compressed requirements into
                  independent subtasks, each with an estimated token budget.
  2. ROUTE      — Assign the cheapest Bedrock model whose context window fits
                  the subtask's token budget.
  3. EXECUTE    — Run all subtasks concurrently using asyncio.gather().
  4. ASSEMBLE   — Join the outputs with clear file-path headers.

Model tiers (configurable via .env):
  ROUTER_MODEL_HAIKU   — cheapest, small context  (default: us.claude-haiku-4-5)
  ROUTER_MODEL_SONNET  — mid-tier fallback         (default: us.claude-sonnet-4-5)

Token budget thresholds (tokens in the subtask prompt + expected output):
  ≤ HAIKU_MAX_TOKENS   → Haiku
  >  HAIKU_MAX_TOKENS  → Sonnet
"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import AsyncGenerator

from bedrock_client import invoke_claude_sync, stream_claude_chat

# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

HAIKU_MODEL = os.getenv(
    "ROUTER_MODEL_HAIKU",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)
SONNET_MODEL = os.getenv(
    "ROUTER_MODEL_SONNET",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
)

# If a subtask's estimated input tokens exceed this, escalate to Sonnet
HAIKU_TOKEN_LIMIT = int(os.getenv("HAIKU_TOKEN_LIMIT", "600"))

# Max output tokens per subtask. Code generation is verbose; at 2048 (and even
# 4096) every subtask was truncated mid-file. Claude Haiku 4.5 supports a much
# larger output budget, so 8192 lets a typical file finish. A small task still
# stops early (the model only uses what it needs), so this caps cost only for
# genuinely large files. Configurable via .env.
SUBTASK_MAX_TOKENS = int(os.getenv("SUBTASK_MAX_TOKENS", "8192"))
# The decomposer only emits a short JSON plan, so it needs far fewer tokens.
DECOMPOSER_MAX_TOKENS = int(os.getenv("DECOMPOSER_MAX_TOKENS", "1024"))

# ---------------------------------------------------------------------------
# Relative cost weights (per token) used to compute meaningful cost savings.
# These are RELATIVE multipliers, not dollar prices — they mirror the public
# Bedrock price ratio between the tiers (Sonnet ≈ 3x the cost of Haiku for
# both input and output tokens). Override via .env if pricing changes.
# ---------------------------------------------------------------------------
HAIKU_COST_PER_TOKEN = float(os.getenv("HAIKU_COST_PER_TOKEN", "1.0"))
SONNET_COST_PER_TOKEN = float(os.getenv("SONNET_COST_PER_TOKEN", "3.0"))


def _cost_per_token(model_id: str) -> float:
    """Relative per-token cost weight for a given model id."""
    return SONNET_COST_PER_TOKEN if model_id == SONNET_MODEL else HAIKU_COST_PER_TOKEN


# ---------------------------------------------------------------------------
# Real USD pricing (per 1,000,000 tokens) — Anthropic Claude 4.5 tier on
# Bedrock. Input and output are priced DIFFERENTLY (output is 5x input for
# Haiku), which is exactly why an input/output cost split matters. Override any
# of these via .env if AWS pricing changes.
#   Haiku 4.5 : $1.00 / 1M input, $5.00 / 1M output
#   Sonnet 4.5: $3.00 / 1M input, $15.00 / 1M output
# ---------------------------------------------------------------------------
HAIKU_USD_PER_1M_INPUT = float(os.getenv("HAIKU_USD_PER_1M_INPUT", "1.0"))
HAIKU_USD_PER_1M_OUTPUT = float(os.getenv("HAIKU_USD_PER_1M_OUTPUT", "5.0"))
SONNET_USD_PER_1M_INPUT = float(os.getenv("SONNET_USD_PER_1M_INPUT", "3.0"))
SONNET_USD_PER_1M_OUTPUT = float(os.getenv("SONNET_USD_PER_1M_OUTPUT", "15.0"))


def _usd_prices(model_id: str) -> tuple[float, float]:
    """Return (input_usd_per_token, output_usd_per_token) for a model id."""
    if model_id == SONNET_MODEL:
        return SONNET_USD_PER_1M_INPUT / 1_000_000, SONNET_USD_PER_1M_OUTPUT / 1_000_000
    return HAIKU_USD_PER_1M_INPUT / 1_000_000, HAIKU_USD_PER_1M_OUTPUT / 1_000_000

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Subtask:
    id: int
    title: str               # e.g. "Database schema"
    prompt: str              # self-contained prompt for this subtask
    estimated_tokens: int    # rough input token estimate
    assigned_model: str = field(default="")
    output: str = field(default="")
    status: str = field(default="pending")  # pending | running | done | error
    real_input_tokens: int = field(default=0)   # actual Bedrock input tokens
    real_output_tokens: int = field(default=0)  # actual Bedrock output tokens

@dataclass
class DecompositionResult:
    subtasks: list[Subtask]
    total_estimated_tokens: int
    savings_vs_single_call: int   # tokens saved by splitting

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

DECOMPOSER_SYSTEM = """\
You are a software project decomposer. Given compressed project requirements, \
split the work into independent, self-contained coding subtasks.

Rules:
- Output ONLY a valid JSON array. No markdown fences, no explanation.
- Each item must have exactly these keys:
    "id":               integer starting at 1
    "title":            short label, e.g. "Database schema"
    "prompt":           A concise, self-contained coding instruction (max 3 sentences).
                        Include only the essential context. Do NOT write long paragraphs.
    "estimated_tokens": integer estimate of total tokens (prompt + expected code output).
- Aim for 4 to 6 subtasks. Good split points: DB schema, API routes, frontend UI,
  auth logic, deployment config.
- Scope each subtask to a SINGLE file or one tightly-related pair of files, so its
  generated code stays focused and complete. If an area is large (e.g. "API routes"
  or "auth"), split it into separate subtasks rather than bundling many files into one.
- Every "prompt" MUST end with this exact instruction: "Keep the implementation
  focused and complete; do not pad with extra examples or boilerplate."
- Keep every "prompt" field under 100 words.
- Keep each subtask's estimated_tokens under 600 where possible.
"""

SUBTASK_SYSTEM = """\
You are an expert software engineer. Generate ONLY the code for the specific task described. \
Be complete and production-ready, but stay focused: implement exactly what the task asks \
and do NOT pad with extra examples, alternative implementations, or unrelated boilerplate. \
Use clear file path comments like `// src/app/page.tsx` above each file's code block. \
No explanations outside of code comments.\
"""

# ---------------------------------------------------------------------------
# Step 1: Decompose
# ---------------------------------------------------------------------------

async def decompose(compressed_requirements: str) -> tuple[list[Subtask], dict]:
    """
    Ask Haiku to break the requirements into a JSON array of subtasks.
    Returns (subtasks, decomposition_usage) where usage holds the real Bedrock
    input/output token counts spent on the decomposition call itself.
    """
    prompt = (
        f"Decompose these project requirements into independent coding subtasks:\n\n"
        f"{compressed_requirements}"
    )

    raw, decomp_usage = await invoke_claude_sync(
        system=DECOMPOSER_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        model_id=HAIKU_MODEL,
        max_tokens=DECOMPOSER_MAX_TOKENS,
    )

    # Strip markdown fences (```json ... ``` or ``` ... ```)
    import re as _re
    cleaned = raw.strip()
    cleaned = _re.sub(r'^```[a-z]*\n?', '', cleaned)
    cleaned = _re.sub(r'\n?```$', '', cleaned.strip()).strip()

    try:
        items = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: treat entire requirements as a single subtask
        items = [{
            "id": 1,
            "title": "Full implementation",
            "prompt": compressed_requirements,
            "estimated_tokens": 800,
        }]

    subtasks = [
        Subtask(
            id=item.get("id", i + 1),
            title=item.get("title", f"Subtask {i + 1}"),
            prompt=item.get("prompt", compressed_requirements),
            estimated_tokens=int(item.get("estimated_tokens", 500)),
        )
        for i, item in enumerate(items)
    ]
    return subtasks, decomp_usage

# ---------------------------------------------------------------------------
# Step 2: Route — assign cheapest model that fits the token budget
# ---------------------------------------------------------------------------

def route(subtasks: list[Subtask]) -> list[Subtask]:
    """
    Assign a model to each subtask based on its estimated token count.
    Mutates subtasks in place and returns them.
    """
    for task in subtasks:
        if task.estimated_tokens <= HAIKU_TOKEN_LIMIT:
            task.assigned_model = HAIKU_MODEL
        else:
            task.assigned_model = SONNET_MODEL
    return subtasks

# ---------------------------------------------------------------------------
# Step 3: Execute subtasks in parallel
# ---------------------------------------------------------------------------

async def _execute_subtask(
    task: Subtask,
    on_progress: callable,
) -> Subtask:
    """
    Run a single subtask against its assigned model (non-streaming).
    Calls on_progress(task) after each status change.
    """
    task.status = "running"
    await on_progress(task)

    try:
        task.output, usage = await invoke_claude_sync(
            system=SUBTASK_SYSTEM,
            messages=[{"role": "user", "content": task.prompt}],
            model_id=task.assigned_model,
            max_tokens=SUBTASK_MAX_TOKENS,
        )
        task.real_input_tokens = usage.get("input_tokens", 0)
        task.real_output_tokens = usage.get("output_tokens", 0)
        task.status = "done"
    except Exception as e:
        task.output = f"// Error in subtask '{task.title}': {e}\n"
        task.status = "error"

    await on_progress(task)
    return task


async def execute_parallel(
    subtasks: list[Subtask],
    on_progress: callable,
) -> list[Subtask]:
    """
    Run all subtasks concurrently. on_progress is called whenever a subtask
    changes status so the caller can stream progress events to the frontend.
    """
    results = await asyncio.gather(
        *[_execute_subtask(task, on_progress) for task in subtasks],
        return_exceptions=False,
    )
    return list(results)

# ---------------------------------------------------------------------------
# Step 4: Assemble final output
# ---------------------------------------------------------------------------

def assemble(subtasks: list[Subtask]) -> str:
    """
    Join subtask outputs with clear section headers.
    """
    parts = []
    for task in subtasks:
        header = f"\n{'='*60}\n## {task.id}. {task.title}  [{task.assigned_model.split('.')[-1]}]\n{'='*60}\n"
        parts.append(header + task.output)
    return "\n".join(parts)

# ---------------------------------------------------------------------------
# Public entry point — streams events back to the caller
# ---------------------------------------------------------------------------

async def run_task_router(
    compressed_requirements: str,
    emit: callable,
    uncompressed_input_tokens: int = 0,
) -> str:
    """
    Full pipeline: decompose → route → execute in parallel → assemble.

    `emit(event_dict)` is called at each stage so the FastAPI endpoint can
    forward data annotations to the frontend in real time.

    `uncompressed_input_tokens` is the heuristic token count of the ORIGINAL
    (pre-compression) requirements, used only to attribute how much of the
    savings came from the input-compression lever.

    Returns the final assembled code string.
    """

    # ── Step 1: Decompose ────────────────────────────────────────────────
    await emit({"event": "router_status", "stage": "decomposing", "message": "Breaking task into subtasks…"})
    subtasks, decomp_usage = await decompose(compressed_requirements)

    # ── Step 2: Route ────────────────────────────────────────────────────
    subtasks = route(subtasks)

    total_estimated = sum(t.estimated_tokens for t in subtasks)

    # ── Truthful "savings vs single call" ────────────────────────────────
    # Baseline: run the ENTIRE workload as one monolithic call on the premium
    # model (Sonnet). Routed: run each subtask on its assigned (usually cheaper)
    # model. Savings is the difference in cost-weighted tokens — positive
    # exactly when routing pushes work onto cheaper tiers. Costs are RELATIVE
    # weights (see _cost_per_token), so we express the result as a
    # token-equivalent figure plus a percentage for the UI.
    baseline_cost = total_estimated * SONNET_COST_PER_TOKEN
    routed_cost = sum(
        t.estimated_tokens * _cost_per_token(t.assigned_model) for t in subtasks
    )
    cost_saved = baseline_cost - routed_cost
    # Convert the relative-cost saving back into "equivalent premium tokens"
    # so the number is intuitive to a human (tokens, not abstract cost units).
    savings_token_equiv = (
        int(round(cost_saved / SONNET_COST_PER_TOKEN)) if SONNET_COST_PER_TOKEN else 0
    )
    savings_pct = round(cost_saved / baseline_cost, 4) if baseline_cost else 0.0

    await emit({
        "event": "router_plan",
        "subtasks": [
            {
                "id": t.id,
                "title": t.title,
                "estimatedTokens": t.estimated_tokens,
                "model": t.assigned_model.split(".")[-1],  # short label
                "status": t.status,
            }
            for t in subtasks
        ],
        "totalEstimatedTokens": total_estimated,
        "savingsVsSingleCall": savings_token_equiv,
        "savingsVsSingleCallPct": savings_pct,
    })

    # ── Step 3: Execute in parallel ──────────────────────────────────────
    async def on_progress(task: Subtask):
        await emit({
            "event": "subtask_update",
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "model": task.assigned_model.split(".")[-1],
        })

    await emit({"event": "router_status", "stage": "executing", "message": f"Running {len(subtasks)} subtasks in parallel…"})
    completed = await execute_parallel(subtasks, on_progress)

    # ── Step 4: Assemble ─────────────────────────────────────────────────
    await emit({"event": "router_status", "stage": "assembling", "message": "Assembling final output…"})
    result = assemble(completed)

    # ── Real Bedrock usage ───────────────────────────────────────────────
    # Aggregate the ACTUAL token counts AWS reported (invocation metrics),
    # across the decomposition call plus every subtask. These are the numbers
    # AWS bills on — not heuristic estimates — so the cost figure is credible.
    real_input = decomp_usage.get("input_tokens", 0) + sum(
        t.real_input_tokens for t in completed
    )
    real_output = decomp_usage.get("output_tokens", 0) + sum(
        t.real_output_tokens for t in completed
    )
    # Relative cost, weighted by each subtask's model tier (decomposition runs
    # on Haiku). Expressed in the same relative units as _cost_per_token.
    real_cost_units = (
        decomp_usage.get("input_tokens", 0) + decomp_usage.get("output_tokens", 0)
    ) * HAIKU_COST_PER_TOKEN + sum(
        (t.real_input_tokens + t.real_output_tokens) * _cost_per_token(t.assigned_model)
        for t in completed
    )

    # Real USD cost, split by input vs output (they are priced differently).
    # Decomposition runs on Haiku; each subtask uses its assigned model.
    d_in_price, d_out_price = _usd_prices(HAIKU_MODEL)
    real_input_cost = decomp_usage.get("input_tokens", 0) * d_in_price
    real_output_cost = decomp_usage.get("output_tokens", 0) * d_out_price
    for t in completed:
        in_price, out_price = _usd_prices(t.assigned_model)
        real_input_cost += t.real_input_tokens * in_price
        real_output_cost += t.real_output_tokens * out_price
    real_total_cost = real_input_cost + real_output_cost

    await emit({
        "event": "real_usage",
        "realInputTokens": real_input,
        "realOutputTokens": real_output,
        "realTotalTokens": real_input + real_output,
        "realCostUnits": round(real_cost_units, 1),
        "realInputCostUsd": round(real_input_cost, 6),
        "realOutputCostUsd": round(real_output_cost, 6),
        "realTotalCostUsd": round(real_total_cost, 6),
        "perSubtask": [
            {
                "id": t.id,
                "title": t.title,
                "model": t.assigned_model.split(".")[-1],
                "inputTokens": t.real_input_tokens,
                "outputTokens": t.real_output_tokens,
                "costUsd": round(
                    t.real_input_tokens * _usd_prices(t.assigned_model)[0]
                    + t.real_output_tokens * _usd_prices(t.assigned_model)[1],
                    6,
                ),
            }
            for t in completed
        ],
    })

    # ── Honest combined savings vs a naive baseline ──────────────────────
    # Baseline = the naive way: ONE call to the premium model (Sonnet) with the
    # UNCOMPRESSED prompt, producing the same output volume. We then attribute
    # the savings to the two levers, and by construction the two attributed
    # amounts sum exactly to (baseline − actual):
    #   • Routing lever    — running each subtask on its cheaper assigned model
    #                        instead of Sonnet, on the same real tokens.
    #   • Compression lever — sending fewer input tokens (priced at the premium
    #                        rate, since the baseline would have paid Sonnet for
    #                        every one of them).
    sonnet_in, sonnet_out = _usd_prices(SONNET_MODEL)

    # Real input tokens actually sent (compressed), across decomp + subtasks.
    real_in_sent = real_input
    # If we don't know the uncompressed size, fall back to the sent size so the
    # compression lever is simply 0 rather than negative.
    uncompressed_in = max(uncompressed_input_tokens, real_in_sent)

    baseline_cost = uncompressed_in * sonnet_in + real_output * sonnet_out

    # Routing lever: what we'd have paid at Sonnet rates on the SAME real tokens
    # minus what we actually paid at each subtask's model rate.
    cost_at_sonnet_same_tokens = real_input * sonnet_in + real_output * sonnet_out
    routing_saving = cost_at_sonnet_same_tokens - real_total_cost
    # Compression lever: the input tokens we never sent, valued at Sonnet input.
    compression_saving = (uncompressed_in - real_in_sent) * sonnet_in

    total_saving = baseline_cost - real_total_cost
    savings_pct = round(total_saving / baseline_cost, 4) if baseline_cost else 0.0

    await emit({
        "event": "savings_breakdown",
        "baselineCostUsd": round(baseline_cost, 6),
        "actualCostUsd": round(real_total_cost, 6),
        "totalSavingUsd": round(total_saving, 6),
        "savingsPct": savings_pct,
        "compressionSavingUsd": round(compression_saving, 6),
        "routingSavingUsd": round(routing_saving, 6),
        "baselineModel": SONNET_MODEL.split(".")[-1],
    })

    await emit({"event": "router_done", "subtaskCount": len(completed)})
    return result
