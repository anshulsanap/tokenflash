"""
local_client.py — Local LLM integration via Ollama

Mirrors the public interface of bedrock_client.py so the rest of the app
(task_router, main) can call either provider interchangeably:

    invoke_local_sync(system, messages, model_id, max_tokens) -> (text, usage)
    stream_local_chat(system, messages, max_tokens)           -> async token stream
    stream_local_response(prompt, max_tokens)                 -> async token stream

Everything runs on the user's own machine through Ollama's local HTTP API
(default http://localhost:11434) — NO cloud, NO API tokens billed. This is the
whole point of the project: run capable models locally for $0 and optimise
tokens so a small local model does more.

Real local token usage is read from Ollama's response fields:
    prompt_eval_count  -> input tokens
    eval_count         -> output tokens

Env config:
    OLLAMA_HOST          (default http://localhost:11434)
    ROUTER_MODEL_LIGHT   (default llama3.2:3b)
    ROUTER_MODEL_HEAVY   (default qwen2.5-coder:7b)
    LOCAL_MODEL          (default = ROUTER_MODEL_HEAVY) — default model
"""

import json
import os
from typing import AsyncGenerator

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")

ROUTER_MODEL_LIGHT = os.getenv("ROUTER_MODEL_LIGHT", "llama3.2:3b")
ROUTER_MODEL_HEAVY = os.getenv("ROUTER_MODEL_HEAVY", "qwen2.5-coder:7b")

# Default model when none is specified (chat/elicitation uses this).
LOCAL_MODEL = os.getenv("LOCAL_MODEL", ROUTER_MODEL_HEAVY)

# Generous timeouts — local generation can be slower than a cloud API,
# especially on first load when the model is paged into memory.
_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=600.0)


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------

def _build_ollama_messages(system: str, messages: list[dict]) -> list[dict]:
    """
    Ollama's /api/chat takes an OpenAI-style messages array with a leading
    system message. Claude Messages format ({role, content}) maps directly.
    """
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):  # normalise structured content
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        out.append({"role": role, "content": content})
    return out


# ---------------------------------------------------------------------------
# Non-streaming invoke (used by the task router)
# ---------------------------------------------------------------------------

async def invoke_local_sync(
    system: str,
    messages: list[dict],
    model_id: str | None = None,
    max_tokens: int = 2048,
) -> tuple[str, dict]:
    """
    Non-streaming local chat completion via Ollama.

    Returns (response_text, usage) where usage carries REAL local token counts:
        {"input_tokens": int, "output_tokens": int}
    These are measured by the local model itself — no cloud, no billing.
    """
    model = model_id or LOCAL_MODEL
    payload = {
        "model": model,
        "messages": _build_ollama_messages(system, messages),
        "stream": False,
        "options": {"num_predict": max_tokens},
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(f"{OLLAMA_HOST}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()

    text = data.get("message", {}).get("content", "")
    usage = {
        "input_tokens": int(data.get("prompt_eval_count", 0)),
        "output_tokens": int(data.get("eval_count", 0)),
    }
    return text, usage


# ---------------------------------------------------------------------------
# Streaming chat (used by the elicitation phase)
# ---------------------------------------------------------------------------

async def stream_local_chat(
    system: str,
    messages: list[dict],
    max_tokens: int = 1024,
    model_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """
    Streaming local chat via Ollama. Yields text chunks as they are generated.
    """
    model = model_id or LOCAL_MODEL
    payload = {
        "model": model,
        "messages": _build_ollama_messages(system, messages),
        "stream": True,
        "options": {"num_predict": max_tokens},
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", f"{OLLAMA_HOST}/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = chunk.get("message", {}).get("content", "")
                if token:
                    yield token
                if chunk.get("done"):
                    break


async def stream_local_response(
    prompt: str,
    max_tokens: int = 4096,
    model_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """
    Single-prompt streaming local completion. Thin wrapper over stream_local_chat
    with a single user message, matching bedrock_client.stream_bedrock_response.
    """
    async for token in stream_local_chat(
        system="",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        model_id=model_id,
    ):
        yield token


# ---------------------------------------------------------------------------
# Health / availability helpers
# ---------------------------------------------------------------------------

async def list_local_models() -> list[str]:
    """Return the model names currently available in the local Ollama server."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        resp = await client.get(f"{OLLAMA_HOST}/api/tags")
        resp.raise_for_status()
        data = resp.json()
    return [m.get("name", "") for m in data.get("models", [])]


async def local_available() -> bool:
    """True if the local Ollama server is reachable."""
    try:
        await list_local_models()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Standalone smoke test:  python local_client.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio

    async def _test():
        print(f"Ollama host: {OLLAMA_HOST}")
        print(f"Light: {ROUTER_MODEL_LIGHT}  Heavy: {ROUTER_MODEL_HEAVY}\n")
        if not await local_available():
            print("❌ Ollama not reachable. Is it running? (ollama serve)")
            return
        print("Available models:", await list_local_models(), "\n")

        print("=== non-streaming invoke ===")
        text, usage = await invoke_local_sync(
            system="You are terse.",
            messages=[{"role": "user", "content": "List 3 colors."}],
            max_tokens=64,
        )
        print("text:", repr(text[:120]))
        print("real local usage:", usage, "\n")

        print("=== streaming chat ===")
        async for tok in stream_local_chat(
            system="You are a friendly architect. Ask one question.",
            messages=[{"role": "user", "content": "I want to build a todo app."}],
            max_tokens=128,
        ):
            print(tok, end="", flush=True)
        print()

    asyncio.run(_test())
