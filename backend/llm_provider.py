"""
llm_provider.py — Provider dispatch layer

A single seam so the rest of the app doesn't care whether the LLM runs locally
(Ollama, default) or in the cloud (AWS Bedrock, optional fallback). Selected via:

    LLM_PROVIDER = local | bedrock      (default: local)

The whole point of the project is to run LOCALLY and spend $0 in API tokens, so
`local` is the default. Bedrock remains available as an explicit opt-in fallback.

Public interface (matches what task_router / main import):
    async invoke_sync(system, messages, model_id=None, max_tokens=2048) -> (text, usage)
    async stream_chat(system, messages, max_tokens=1024)                -> async str
    async stream_response(prompt, max_tokens=4096)                      -> async str
    PROVIDER            -> "local" | "bedrock"
    is_local()          -> bool
"""

import os
from typing import AsyncGenerator

PROVIDER = os.getenv("LLM_PROVIDER", "local").strip().lower()


def is_local() -> bool:
    return PROVIDER != "bedrock"


# ---------------------------------------------------------------------------
# invoke_sync — non-streaming, returns (text, usage)
# ---------------------------------------------------------------------------

async def invoke_sync(
    system: str,
    messages: list[dict],
    model_id: str | None = None,
    max_tokens: int = 2048,
) -> tuple[str, dict]:
    if is_local():
        from local_client import invoke_local_sync
        return await invoke_local_sync(system, messages, model_id, max_tokens)
    from bedrock_client import invoke_claude_sync
    return await invoke_claude_sync(system, messages, model_id, max_tokens)


# ---------------------------------------------------------------------------
# stream_chat — multi-turn streaming (elicitation)
# ---------------------------------------------------------------------------

async def stream_chat(
    system: str,
    messages: list[dict],
    max_tokens: int = 1024,
) -> AsyncGenerator[str, None]:
    if is_local():
        from local_client import stream_local_chat
        async for tok in stream_local_chat(system, messages, max_tokens):
            yield tok
    else:
        from bedrock_client import stream_claude_chat
        async for tok in stream_claude_chat(system, messages, max_tokens):
            yield tok


# ---------------------------------------------------------------------------
# stream_response — single-prompt streaming
# ---------------------------------------------------------------------------

async def stream_response(
    prompt: str,
    max_tokens: int = 4096,
) -> AsyncGenerator[str, None]:
    if is_local():
        from local_client import stream_local_response
        async for tok in stream_local_response(prompt, max_tokens):
            yield tok
    else:
        from bedrock_client import stream_bedrock_response
        async for tok in stream_bedrock_response(prompt, max_tokens):
            yield tok
