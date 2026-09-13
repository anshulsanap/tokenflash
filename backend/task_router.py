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

from llm_provider import invoke_sync, is_local

# ---------------------------------------------------------------------------
# Model registry — provider-aware
#
# The project runs LOCALLY by default (Ollama), so the two routing tiers map to
# a light and a heavy LOCAL model. When LLM_PROVIDER=bedrock, they fall back to
# the cloud Claude tiers. The internal names LIGHT_MODEL / HEAVY_MODEL replace
# the old Haiku/Sonnet naming to reflect that these are just "cheap vs capable"
# tiers regardless of provider.
# ---------------------------------------------------------------------------

if is_local():
    LIGHT_MODEL = os.getenv("ROUTER_MODEL_LIGHT", "llama3.2:3b")
    HEAVY_MODEL = os.getenv("ROUTER_MODEL_HEAVY", "qwen2.5-coder:7b")
else:
    LIGHT_MODEL = os.getenv(
        "ROUTER_MODEL_HAIKU", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    HEAVY_MODEL = os.getenv(
        "ROUTER_MODEL_SONNET", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )

# Backward-compatible aliases (older code / tests referenced these names).
HAIKU_MODEL = LIGHT_MODEL
SONNET_MODEL = HEAVY_MODEL

# If a subtask's estimated input tokens exceed this, escalate to the heavy tier
HAIKU_TOKEN_LIMIT = int(os.getenv("HAIKU_TOKEN_LIMIT", "600"))

# Max output tokens per subtask. Code generation is verbose; keep generous so a
# file finishes instead of truncating. Local models simply take a bit longer;
# there is no per-token bill. Configurable via .env.
SUBTASK_MAX_TOKENS = int(os.getenv("SUBTASK_MAX_TOKENS", "8192"))
# The decomposer only emits a short JSON plan, so it needs far fewer tokens.
DECOMPOSER_MAX_TOKENS = int(os.getenv("DECOMPOSER_MAX_TOKENS", "1024"))

# ---------------------------------------------------------------------------
# Relative cost weights (per token) — used only for the routing-savings story.
# Local models cost $0 in API fees, but the heavy tier still "costs" more in
# compute/time, so we keep a relative weight so routing to the light model
# still shows as a saving. For bedrock these mirror the cloud price ratio.
# ---------------------------------------------------------------------------
HAIKU_COST_PER_TOKEN = float(os.getenv("HAIKU_COST_PER_TOKEN", "1.0"))
SONNET_COST_PER_TOKEN = float(os.getenv("SONNET_COST_PER_TOKEN", "3.0"))


def _cost_per_token(model_id: str) -> float:
    """Relative per-token cost weight for a given model id."""
    return SONNET_COST_PER_TOKEN if model_id == HEAVY_MODEL else HAIKU_COST_PER_TOKEN


def _short_model(model_id: str) -> str:
    """
    Human-friendly short label for a model id, for BOTH providers.

    Local Ollama names contain dots (llama3.2:3b, qwen2.5-coder:7b) so the old
    `split('.')[-1]` mangled them into '2:3b' / '5-coder:7b'. For cloud Bedrock
    ids like 'us.anthropic.claude-haiku-4-5-...' we still want the last segment.
    """
    if not model_id:
        return ""
    # Local Ollama models are shown as-is (already short, e.g. 'llama3.2:3b').
    if ":" in model_id and model_id.count(".") <= 1:
        return model_id
    # Cloud/Bedrock dotted ids → last dotted segment.
    return model_id.split(".")[-1]


# ---------------------------------------------------------------------------
# Real USD pricing (per 1,000,000 tokens).
#
# LOCAL provider: API cost is $0 — the model runs on the user's machine. That
# is the entire value proposition, so local prices are zero.
#
# We ALSO keep the cloud (Bedrock/Claude) prices around as a reference so the
# UI can answer "what would this have cost on the cloud?" — turning the local
# run's real token counts into an "amount you DIDN'T pay" figure.
#   Haiku 4.5 : $1.00 / 1M input, $5.00 / 1M output
#   Sonnet 4.5: $3.00 / 1M input, $15.00 / 1M output
# ---------------------------------------------------------------------------
HAIKU_USD_PER_1M_INPUT = float(os.getenv("HAIKU_USD_PER_1M_INPUT", "1.0"))
HAIKU_USD_PER_1M_OUTPUT = float(os.getenv("HAIKU_USD_PER_1M_OUTPUT", "5.0"))
SONNET_USD_PER_1M_INPUT = float(os.getenv("SONNET_USD_PER_1M_INPUT", "3.0"))
SONNET_USD_PER_1M_OUTPUT = float(os.getenv("SONNET_USD_PER_1M_OUTPUT", "15.0"))


def _usd_prices(model_id: str) -> tuple[float, float]:
    """
    Real (input, output) USD-per-token actually paid. For local models this is
    (0, 0) — nothing is billed. For bedrock it's the tier's cloud price.
    """
    if is_local():
        return 0.0, 0.0
    if model_id == HEAVY_MODEL:
        return SONNET_USD_PER_1M_INPUT / 1_000_000, SONNET_USD_PER_1M_OUTPUT / 1_000_000
    return HAIKU_USD_PER_1M_INPUT / 1_000_000, HAIKU_USD_PER_1M_OUTPUT / 1_000_000


def _cloud_equivalent_prices(model_id: str) -> tuple[float, float]:
    """
    What the SAME work would cost on the cloud (Claude), used to show the
    "you avoided paying $X by running locally" figure. Always returns cloud
    prices regardless of provider.
    """
    if model_id == HEAVY_MODEL:
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

FIRST, infer WHAT KIND of software this is (e.g. CLI tool, web app, API/service, \
library, game, script, data pipeline, bot) and choose split points that FIT that \
kind. Do NOT force web-app structure onto everything. Use the SAME language and \
tech the requirements specify — never introduce a database, web framework, or UI \
that the user did not ask for.

Rules:
- Output ONLY a valid JSON array. No markdown fences, no explanation.
- Each item must have exactly these keys:
    "id":               integer starting at 1
    "title":            short label describing THIS project's part
    "prompt":           A concise, self-contained coding instruction (max 3 sentences).
                        Include only the essential context. Do NOT write long paragraphs.
    "estimated_tokens": integer estimate of total tokens (prompt + expected code output).
- Aim for 4 to 6 subtasks. Choose split points APPROPRIATE TO THE PROJECT TYPE, e.g.:
    • CLI tool     → argument parsing, core logic, file/IO handling, tests, README
    • Web app      → data model, API routes, frontend UI, auth, deployment config
    • Library/pkg  → public API, core modules, error handling, tests, packaging
    • Game         → game loop, entities/state, input handling, rendering, scoring
    • Script/ETL   → input parsing, transform logic, output/writer, CLI/config
  These are examples — derive the right split from what the user actually asked for.
- Scope each subtask to a SINGLE file or one tightly-related pair of files, so its
  generated code stays focused and complete. If an area is large, split it further
  rather than bundling many files into one.
- Every "prompt" MUST end with this exact instruction: "Keep the implementation
  focused and complete; do not pad with extra examples or boilerplate."
- Keep every "prompt" field under 100 words.
- Keep each subtask's estimated_tokens under 600 where possible.
"""

SUBTASK_SYSTEM = """\
You are an expert software engineer. Generate ONLY the code for the specific task described. \
Use the SAME programming language and technology the task specifies — do not switch \
languages or introduce frameworks/databases the task did not ask for. \
Be complete and production-ready, but stay focused: implement exactly what the task asks \
and do NOT pad with extra examples, alternative implementations, or unrelated boilerplate. \
Put a file-path comment above each file's code block using that language's comment syntax \
and a path appropriate to the project (e.g. `# rename_tool/cli.py` or `// src/index.js`). \
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

    raw, decomp_usage = await invoke_sync(
        system=DECOMPOSER_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        model_id=LIGHT_MODEL,
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
            task.assigned_model = LIGHT_MODEL
        else:
            task.assigned_model = HEAVY_MODEL
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
        task.output, usage = await invoke_sync(
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
                "model": _short_model(t.assigned_model),  # short label
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
            "model": _short_model(task.assigned_model),
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
                "model": _short_model(t.assigned_model),
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

    # ── Savings vs the cloud "naive baseline" ────────────────────────────
    # Baseline = the naive expensive way: ONE call to the premium CLOUD model
    # with the UNCOMPRESSED prompt, at cloud prices — i.e. what a student/small
    # team would have paid without this tool. `actual` is what THIS run really
    # cost: $0 when running locally (Ollama), or the routed cloud cost in
    # bedrock mode. The gap is the money avoided.
    #
    # We also attribute WHERE the saving comes from:
    #   • Local/routing lever — running on a local (or cheaper) model instead of
    #                           the premium cloud model, on the same real tokens.
    #   • Compression lever   — the input tokens we never had to process.
    prem_in, prem_out = _cloud_equivalent_prices(HEAVY_MODEL)  # premium cloud tier

    real_in_sent = real_input
    uncompressed_in = max(uncompressed_input_tokens, real_in_sent)

    baseline_cost = uncompressed_in * prem_in + real_output * prem_out

    # Cloud-equivalent cost of the SAME real tokens on the premium cloud model.
    cloud_same_tokens = real_input * prem_in + real_output * prem_out
    # Local/routing lever: premium-cloud cost of those tokens minus what we
    # actually paid (0 locally, or the routed cloud price in bedrock mode).
    routing_saving = cloud_same_tokens - real_total_cost
    # Compression lever: the input tokens we never sent, at premium cloud input.
    compression_saving = (uncompressed_in - real_in_sent) * prem_in

    total_saving = baseline_cost - real_total_cost
    savings_pct = round(total_saving / baseline_cost, 4) if baseline_cost else 0.0

    await emit({
        "event": "savings_breakdown",
        "provider": "local" if is_local() else "bedrock",
        "baselineCostUsd": round(baseline_cost, 6),
        "actualCostUsd": round(real_total_cost, 6),
        "totalSavingUsd": round(total_saving, 6),
        "savingsPct": savings_pct,
        "compressionSavingUsd": round(compression_saving, 6),
        "routingSavingUsd": round(routing_saving, 6),
        "baselineModel": _short_model(HEAVY_MODEL),
    })

    await emit({"event": "router_done", "subtaskCount": len(completed)})
    return result
