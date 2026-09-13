"""
bedrock_client.py — AWS Bedrock Integration

Two public interfaces:

  stream_claude_chat(system, messages, max_tokens)
      Multi-turn Messages API call for Claude models.
      Used by the elicitation phase — passes the full conversation history
      and a system prompt so Claude can hold a real dialogue.

  stream_bedrock_response(prompt, max_tokens)
      Single-prompt streaming call supporting both Claude and Mistral.
      Used by the generation phase (compressed prompt → code output).

Credentials are read from environment variables:
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    AWS_REGION           (default: us-east-1)
    BEDROCK_MODEL_ID     (default: anthropic.claude-3-haiku-20240307-v1:0)
"""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncGenerator

import boto3
from botocore.config import Config
from dotenv import load_dotenv

# Load .env from the directory containing this file (backend/.env or project root .env)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

# ---------------------------------------------------------------------------
# Shared thread pool for running synchronous boto3 calls without blocking
# the uvicorn event loop.
# ---------------------------------------------------------------------------
_THREAD_POOL = ThreadPoolExecutor(max_workers=4)

# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

def _get_bedrock_client():
    """
    Build a bedrock-runtime boto3 client.
    Credentials are injected via environment variables — no hardcoded secrets.
    """
    region = os.getenv("AWS_REGION", "us-east-1").strip()

    # Only pass a session token if one is actually set — an empty string
    # would corrupt the AWS4 signature.
    session_token = os.getenv("AWS_SESSION_TOKEN", "").strip() or None

    return boto3.client(
        service_name="bedrock-runtime",
        region_name=region,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"].strip(),
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"].strip(),
        aws_session_token=session_token,
        config=Config(
            retries={"max_attempts": 3, "mode": "standard"},
            read_timeout=120,
            connect_timeout=10,
        ),
    )


# ---------------------------------------------------------------------------
# Token extraction from raw streaming event bytes
# ---------------------------------------------------------------------------

def _extract_token_claude(chunk_bytes: bytes) -> str:
    """Parse a raw streaming chunk from a Claude Messages API response."""
    try:
        data = json.loads(chunk_bytes)
        if data.get("type") == "content_block_delta":
            return data.get("delta", {}).get("text", "")
    except (json.JSONDecodeError, KeyError):
        pass
    return ""


def _extract_token_mistral(chunk_bytes: bytes) -> str:
    """Parse a raw streaming chunk from a Mistral model response."""
    try:
        data = json.loads(chunk_bytes)
        outputs = data.get("outputs", [])
        if outputs:
            return outputs[0].get("text", "")
    except (json.JSONDecodeError, KeyError):
        pass
    return ""


def _extract_usage(chunk_bytes: bytes) -> dict | None:
    """
    Extract REAL Bedrock token usage from a streaming chunk.

    Bedrock reports the authoritative, billed token counts in the final
    `message_stop` chunk under `amazon-bedrock-invocationMetrics`:
        {"inputTokenCount": 15, "outputTokenCount": 10, ...}

    Claude also reports usage incrementally in `message_delta`
    (`usage.output_tokens`) and `message_start` (`usage.input_tokens`); we
    prefer the invocation metrics since they are the numbers AWS bills on.

    Returns {"input_tokens": int, "output_tokens": int} or None if this chunk
    carries no usage info.
    """
    try:
        data = json.loads(chunk_bytes)
    except (json.JSONDecodeError, TypeError):
        return None

    # Authoritative billed metrics (Claude + most Bedrock models)
    metrics = data.get("amazon-bedrock-invocationMetrics")
    if metrics:
        return {
            "input_tokens": int(metrics.get("inputTokenCount", 0)),
            "output_tokens": int(metrics.get("outputTokenCount", 0)),
        }

    # Fallback: Claude message_delta / message_start usage blocks
    usage = data.get("usage")
    if usage and ("input_tokens" in usage or "output_tokens" in usage):
        return {
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
        }
    return None


# ---------------------------------------------------------------------------
# Synchronous helpers (run in thread pool so they don't block the event loop)
# ---------------------------------------------------------------------------

def _invoke_claude_stream_sync(
    client,
    model_id: str,
    system: str,
    messages: list[dict],
    max_tokens: int,
) -> tuple[list[str], dict]:
    """
    Synchronous boto3 call using the Claude Messages API.

    Returns a tuple of (tokens, usage) where usage carries the REAL Bedrock
    token counts: {"input_tokens": int, "output_tokens": int}.

    'messages' must be in Claude Messages API format:
        [{"role": "user"|"assistant", "content": "<text>"}]
    """
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }

    response = client.invoke_model_with_response_stream(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body),
    )

    tokens: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    for event in response["body"]:
        chunk = event.get("chunk")
        if not chunk:
            continue
        token = _extract_token_claude(chunk["bytes"])
        if token:
            tokens.append(token)
        u = _extract_usage(chunk["bytes"])
        if u:
            # Keep the largest counts seen (message_stop is authoritative)
            usage["input_tokens"] = max(usage["input_tokens"], u["input_tokens"])
            usage["output_tokens"] = max(usage["output_tokens"], u["output_tokens"])
    return tokens, usage


def _invoke_single_prompt_sync(
    client,
    model_id: str,
    prompt: str,
    max_tokens: int,
) -> tuple[list[str], dict]:
    """
    Synchronous boto3 call for a single-turn prompt.
    Supports both Claude (Messages API) and Mistral (instruct format).
    Returns (tokens, usage) with real Bedrock token counts.
    """
    is_mistral = "mistral" in model_id.lower()

    if is_mistral:
        body = {
            "prompt": f"<s>[INST]{prompt}[/INST]",
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "top_p": 0.9,
        }
        extract_fn = _extract_token_mistral
    else:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        extract_fn = _extract_token_claude

    response = client.invoke_model_with_response_stream(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body),
    )

    tokens: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    for event in response["body"]:
        chunk = event.get("chunk")
        if not chunk:
            continue
        token = extract_fn(chunk["bytes"])
        if token:
            tokens.append(token)
        u = _extract_usage(chunk["bytes"])
        if u:
            usage["input_tokens"] = max(usage["input_tokens"], u["input_tokens"])
            usage["output_tokens"] = max(usage["output_tokens"], u["output_tokens"])
    return tokens, usage


# ---------------------------------------------------------------------------
# Public async interfaces
# ---------------------------------------------------------------------------

async def count_tokens(text: str) -> int:
    """
    Use the AWS Bedrock CountTokens API to get an exact input token count.
    This is the metric cited in the paper for real cost measurement.

    Falls back to a word-count approximation if the API call fails
    (e.g. model doesn't support CountTokens, or credentials not ready).
    """
    model_id = os.getenv(
        "BEDROCK_MODEL_ID",
        "anthropic.claude-3-haiku-20240307-v1:0",
    )
    client = _get_bedrock_client()
    loop = asyncio.get_event_loop()

    def _count_sync() -> int:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "messages": [{"role": "user", "content": text}],
        }
        resp = client.count_tokens(
            modelId=model_id,
            messages=body["messages"],
        )
        return resp.get("inputTokenCount", len(text.split()))

    return await loop.run_in_executor(_THREAD_POOL, _count_sync)


async def invoke_claude_sync(
    system: str,
    messages: list[dict],
    model_id: str | None = None,
    max_tokens: int = 2048,
) -> tuple[str, dict]:
    """
    Non-streaming Claude call — collects the full response and returns it
    together with the REAL Bedrock token usage. Used by the task router for
    subtask execution and decomposition where streaming isn't needed.

    Args:
        system:     System prompt.
        messages:   Conversation in Claude Messages API format.
        model_id:   Override the default model (for routing to different tiers).
        max_tokens: Maximum tokens in the response.

    Returns:
        Tuple of (response_text, usage) where usage is
        {"input_tokens": int, "output_tokens": int} from Bedrock's own
        invocation metrics — the numbers AWS actually bills.
    """
    resolved_model = model_id or os.getenv(
        "BEDROCK_MODEL_ID",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    client = _get_bedrock_client()
    loop = asyncio.get_event_loop()

    tokens, usage = await loop.run_in_executor(
        _THREAD_POOL,
        _invoke_claude_stream_sync,
        client,
        resolved_model,
        system,
        messages,
        max_tokens,
    )
    return "".join(tokens), usage


async def stream_claude_chat(
    system: str,
    messages: list[dict],
    max_tokens: int = 1024,
) -> AsyncGenerator[str, None]:
    """
    Multi-turn async streaming chat with Claude via the Messages API.

    Runs the synchronous boto3 call in a thread pool to avoid blocking the
    event loop, then yields collected tokens one at a time.

    Args:
        system:     System prompt string — sets the model's persona/behaviour.
        messages:   Conversation history in Claude Messages API format:
                    [{"role": "user"|"assistant", "content": "<text>"}]
                    The list must end with a user-role message.
        max_tokens: Maximum tokens in the assistant's reply (keep low for chat
                    turns to reduce cost; default 1024 is generous for a question).

    Yields:
        Individual token strings as they are collected.
    """
    model_id = os.getenv(
        "BEDROCK_MODEL_ID",
        "anthropic.claude-3-haiku-20240307-v1:0",
    )
    client = _get_bedrock_client()
    loop = asyncio.get_event_loop()

    tokens, _usage = await loop.run_in_executor(
        _THREAD_POOL,
        _invoke_claude_stream_sync,
        client,
        model_id,
        system,
        messages,
        max_tokens,
    )

    for token in tokens:
        yield token


async def stream_bedrock_response(
    prompt: str,
    max_tokens: int = 4096,
) -> AsyncGenerator[str, None]:
    """
    Single-prompt async streaming call for code generation.

    Supports Claude and Mistral models. The model is chosen via the
    BEDROCK_MODEL_ID environment variable.

    Args:
        prompt:     Fully-assembled (compressed) prompt string.
        max_tokens: Maximum tokens in the model's response.

    Yields:
        Individual token strings.
    """
    model_id = os.getenv(
        "BEDROCK_MODEL_ID",
        "anthropic.claude-3-haiku-20240307-v1:0",
    )
    client = _get_bedrock_client()
    loop = asyncio.get_event_loop()

    tokens, _usage = await loop.run_in_executor(
        _THREAD_POOL,
        _invoke_single_prompt_sync,
        client,
        model_id,
        prompt,
        max_tokens,
    )

    for token in tokens:
        yield token


# ---------------------------------------------------------------------------
# Standalone smoke test  (python bedrock_client.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    async def _test_chat():
        print("=== Multi-turn chat test ===\n")
        system = "You are a friendly technical architect. Ask one clarifying question."
        msgs = [{"role": "user", "content": "I want to build a task management app."}]
        async for tok in stream_claude_chat(system, msgs, max_tokens=256):
            print(tok, end="", flush=True)
        print("\n")

    async def _test_generation():
        print("=== Single-prompt generation test ===\n")
        async for tok in stream_bedrock_response("Say hello in exactly 10 words.", max_tokens=64):
            print(tok, end="", flush=True)
        print("\n")

    asyncio.run(_test_chat())
    asyncio.run(_test_generation())
