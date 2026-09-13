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

from bedrock_client import stream_claude_chat, stream_bedrock_response
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
You are a dynamic, human-like technical architect who helps people build software \
of ANY kind — a website, a CLI tool, an API, a game, an automation script, a data \
pipeline, a bot, a library, whatever they have in mind. A web app is just ONE \
possibility; never assume it. \
You MUST read the user's latest message and react to it. \
If they say stop, change the subject, or reject a feature, you must adapt immediately. \
Do not just loop through a hardcoded list of questions.

Your goal is to gather enough requirements to generate the right code for whatever \
they want to build, through real conversation — not a script.

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

Dimensions to cover WHEN RELEVANT to the kind of project (through conversation, not \
interrogation — skip any that don't apply):
- What the thing is and who/what it's for
- The core features or behaviour it must have
- Language / framework / tech preferences (or sensible defaults)
- Any data, storage, or state it needs
- How it runs or is used (CLI, web, service, library, etc.)
- Any integrations, auth, or deployment needs — only if applicable

Once you have enough to work with — explicit answers or accepted defaults — summarise \
what you have in a short bullet list, then end your message with exactly this line by itself:
REQUIREMENTS_COMPLETE

Do not include REQUIREMENTS_COMPLETE until you genuinely have enough to generate code. \
If the user just wants to go, state the defaults you will use and include REQUIREMENTS_COMPLETE.\
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
            async for token in stream_claude_chat(
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
    return {
        "status": "ok",
        "model": os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    }
