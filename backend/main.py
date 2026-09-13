"""
AI FinOps Router — FastAPI Backend
Vercel AI SDK Data Stream Protocol
https://sdk.vercel.ai/docs/ai-sdk-ui/stream-protocol
"""

import asyncio
import json
import os
from typing import AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

from llm_provider import stream_chat, stream_response, invoke_sync, PROVIDER
from compressor import compress_prompt, compress_prompt_detailed
from task_router import run_task_router

app = FastAPI(title="AI FinOps Router", version="0.1.0")

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
2. Your VERY FIRST question must be open-ended: ask what they want to make/build, \
without assuming it is an app or website. For example: "What do you want to build?" \
Let THEM tell you the kind of thing it is. Only after they answer do you ask about \
details relevant to that specific kind of project.
3. Ask ONE question at a time. Never stack multiple questions in one reply.
4. Do not repeat a question the user has already answered, even partially.
5. Tailor follow-up questions to the KIND of project they described. A CLI tool, a game, \
and a web app need different questions — do not ask about databases, auth, or deployment \
if they make no sense for what the user is building.
6. If the user says "stop", "skip", "I don't care", "doesn't matter", or any equivalent, \
accept it gracefully and move on. Never re-ask a topic they have dismissed.
7. If the user seems frustrated or just wants to get started, acknowledge it and proceed \
with sensible defaults for anything still missing.
8. Be warm, concise, and natural. Match the user's energy and vocabulary.

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


async def classify_task_intent(summary: str) -> str:
    """
    Classify the scoped requirements as 'build' or 'perform'.

    Primary: a quick one-word local model call (cheap, local, $0). Falls back to
    a keyword heuristic if the model returns something unexpected. Defaults to
    'build' to preserve the original behaviour when genuinely ambiguous.
    """
    text = (summary or "").strip()
    if not text:
        return "build"

    # Fast local classification call.
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
        pass  # fall through to heuristic

    # Heuristic fallback.
    low = text.lower()
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

# ---------------------------------------------------------------------------
# /api/chat  — elicitation passthrough
# ---------------------------------------------------------------------------

@app.post("/api/chat")
async def chat(request: Request) -> StreamingResponse:
    body = await request.json()
    messages: list[dict] = body.get("messages", [])
    phase: str = body.get("phase", "elicit")

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

            if "REQUIREMENTS_COMPLETE" in accumulated:
                yield data_annotation({"event": "phase_complete", "nextPhase": "generate"})

            yield finish_message("stop", {"completionTokens": token_count})

        elif phase == "generate":
            summary = extract_requirements_summary(messages)
            detail = compress_prompt_detailed(summary)
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
            # from invisible backend routers.
            yield data_annotation({
                "event": "compression_diff",
                "original": summary,
                "tokens": detail["tokens"],
                "multiplier": stats["multiplier"],
            })

            # ── Task-type routing: PERFORM the task or BUILD a tool ──────────
            intent = await classify_task_intent(summary)
            yield data_annotation({"event": "task_mode", "mode": intent})

            if intent == "perform":
                # Do the knowledge task directly — stream a real answer from the
                # local model. No decomposition, no code pipeline.
                perform_prompt = build_perform_prompt(summary, compressed)
                async for token in stream_chat(
                    system=PERFORM_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": perform_prompt}],
                    max_tokens=2048,
                ):
                    yield text_delta(token)
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

            result_text = await run_task_router(
                compressed,
                collect_emit,
                uncompressed_input_tokens=stats["original_tokens"],
            )

            # Now yield all collected annotations
            for kind, payload in pending_events:
                if kind == "annotation":
                    yield data_annotation(payload)

            # Stream the assembled code
            for i in range(0, len(result_text), 16):
                yield text_delta(result_text[i:i + 16])
                await asyncio.sleep(0)

            yield finish_message("stop")

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
