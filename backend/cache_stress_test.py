"""
cache_stress_test.py — Offline adversarial stress-test dev tool (Req 5)

A LOCAL command-line developer tool (never a served endpoint, Req 5.1) that
quantifies how the hardened cluster-based cache policy resists collision /
cache-poisoning compared to a naive single-threshold policy. It loads an
``Adversarial_Prompt_Set`` — a list of ``{"prompt", "group"}`` items where the
``group`` label marks which prompts SHOULD be considered the same cached answer
— embeds each prompt on-device with the same production model, and for every
query prompt scores it against all OTHER prompts. The SAME score list is fed to
both ``naive_decision`` and ``hardened_decision`` so the comparison is
apples-to-apples, and a ``Wrong_Hit`` is counted whenever a policy "hits" but
the served (top-scoring) candidate belongs to a DIFFERENT group than the query.

Privacy (Req 5.11): every prompt is redacted via ``redactor.redact`` BEFORE it
is embedded, so no raw sensitive value carried in the set ever reaches the
embedding or the report. The report itself contains ONLY counts and rates —
never any prompt text or sensitive value (Req 5.7).

Determinism (Req 5.8): given the same set, config, and model, the report is
identical — embedding is deterministic and every step is pure arithmetic.

Everything imports the SAME production helpers used by the request path
(``load_embedding_model``, ``embed``, ``naive_decision``, ``hardened_decision``
and the ``DEFAULT_*`` constants) so the tool measures the exact production
policy. No network call is made at any point.
"""

from __future__ import annotations

import argparse
import json
import sys

import redactor
import semantic_cache
from semantic_cache import (
    DEFAULT_MARGIN_THRESHOLD,
    DEFAULT_MIN_SIMILARITY,
    DEFAULT_TOP_K,
    embed,
    hardened_decision,
    load_embedding_model,
    naive_decision,
)

# Fixed session id used only so redaction has a session label; it never reaches
# the report (redaction telemetry is not collected here).
_STRESS_SESSION = "stress-test-session"


def load_adversarial_set(path: str) -> list[dict]:
    """Load and validate an ``Adversarial_Prompt_Set`` from a JSON file.

    The file must be a JSON list of ``{"prompt": str, "group": str}`` items.
    Every item MUST carry a non-empty ``group`` label; a missing/empty group is
    rejected with a ``ValueError`` naming the offending item (Req 5.3).

    Each prompt is redacted via ``redactor.redact(...)`` using ONLY the built-in
    regex detectors (no audit/state side-effects), and the REDACTED text is kept
    for embedding so no raw sensitive value carried in the set can reach the
    report (Req 5.11). The original group label is preserved alongside the
    redacted prompt.

    Returns a list of ``{"prompt": <redacted str>, "group": str}``.
    """
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)

    if not isinstance(raw, list):
        raise ValueError("adversarial set must be a JSON list of {prompt, group}")

    cleaned: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"item {index} is not an object with prompt/group")

        prompt = item.get("prompt", "")
        group = item.get("group", "")

        # Req 5.3 — reject the set naming the item missing a non-empty group.
        if not isinstance(group, str) or not group.strip():
            raise ValueError(
                f"item {index} (prompt index {index}) is missing a non-empty 'group' label"
            )

        # Req 5.11 — redact before embedding so no raw sensitive value reaches
        # the report. Built-in regex detectors only; no audit/state needed.
        result = redactor.redact(
            prompt if isinstance(prompt, str) else str(prompt),
            _STRESS_SESSION,
            detectors=redactor.BUILTIN_REGEX_DETECTORS,
            audit=None,
            state=None,
        )
        redacted = result.redacted_text if result.ok else ""

        cleaned.append({"prompt": redacted, "group": group})

    return cleaned


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two unit-length embeddings — a plain dot product.

    ``embed`` normalizes vectors (``normalize_embeddings=True``) so the dot
    product IS the cosine similarity; no re-normalization is needed.
    """
    return sum(x * y for x, y in zip(a, b))


def run_stress_test(
    prompt_set: list[dict],
    *,
    model,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    margin_threshold: float = DEFAULT_MARGIN_THRESHOLD,
) -> dict:
    """Run the naive-vs-hardened wrong-hit comparison over ``prompt_set``.

    For each query prompt, embed it and every OTHER prompt (real model), compute
    the cosine similarity to each other prompt (tracking each candidate's
    group), and feed the SAME score list to ``naive_decision`` and
    ``hardened_decision``. The served entry is the top-scoring candidate; a
    ``Wrong_Hit`` is counted for a policy whenever it decides "hit" and the top
    candidate's group differs from the query group (Req 5.6).

    Deterministic given the same set/config/model (Req 5.8). An empty set (or a
    set with fewer than 2 prompts, where no candidate exists) yields zero
    prompts evaluated and ``0.0`` rates for both policies (Req 5.10). The report
    contains only counts/rates — no prompt text or sensitive value (Req 5.7).

    Returns::

        {"promptsEvaluated", "naiveWrongHits", "naiveWrongHitRate",
         "hardenedWrongHits", "hardenedWrongHitRate"}
    """
    n = len(prompt_set)

    # Empty (or effectively empty) set — nothing to compare (Req 5.10).
    if n == 0:
        return {
            "promptsEvaluated": 0,
            "naiveWrongHits": 0,
            "naiveWrongHitRate": 0.0,
            "hardenedWrongHits": 0,
            "hardenedWrongHitRate": 0.0,
        }

    # Embed every prompt once (deterministic). Keep group labels aligned.
    embeddings = [embed(model, item["prompt"]) for item in prompt_set]
    groups = [item["group"] for item in prompt_set]

    prompts_evaluated = 0
    naive_wrong = 0
    hardened_wrong = 0

    top_k = min(top_k, max(0, n - 1)) if n > 1 else 0

    for i in range(n):
        query_emb = embeddings[i]
        query_group = groups[i]

        # Score against every OTHER prompt, carrying each candidate's group.
        scored: list[tuple[float, str]] = []
        for j in range(n):
            if j == i:
                continue
            scored.append((_cosine(query_emb, embeddings[j]), groups[j]))

        # Every prompt is evaluated, even when it has no candidates (Req 5.10
        # single-item behaviour: no candidate -> both policies miss -> no
        # wrong hit, but the prompt is still counted as evaluated).
        prompts_evaluated += 1

        if not scored:
            continue

        # Restrict to the top_k nearest candidates (descending similarity), to
        # mirror the production lookup's cluster window.
        scored.sort(key=lambda c: c[0], reverse=True)
        window = scored[:top_k] if top_k > 0 else scored

        scores = [s for s, _g in window]
        top_group = window[0][1]  # group of the top-scoring (served) candidate

        naive = naive_decision(scores, min_similarity=min_similarity)
        hardened, _top, _ru, _margin = hardened_decision(
            scores, min_similarity=min_similarity, margin_threshold=margin_threshold
        )

        if naive == "hit" and top_group != query_group:
            naive_wrong += 1
        if hardened == "hit" and top_group != query_group:
            hardened_wrong += 1

    naive_rate = (naive_wrong / prompts_evaluated) if prompts_evaluated > 0 else 0.0
    hardened_rate = (
        (hardened_wrong / prompts_evaluated) if prompts_evaluated > 0 else 0.0
    )

    return {
        "promptsEvaluated": prompts_evaluated,
        "naiveWrongHits": naive_wrong,
        "naiveWrongHitRate": naive_rate,
        "hardenedWrongHits": hardened_wrong,
        "hardenedWrongHitRate": hardened_rate,
    }


def _main(argv: list[str] | None = None) -> int:
    """LOCAL CLI entry point (Req 5.1) — never a served endpoint.

    Loads the production embedding model, reads and validates the adversarial
    set, runs the stress test, and prints the report dict as JSON. Returns a
    non-zero exit code when the model is unavailable so a broken local setup is
    obvious.
    """
    parser = argparse.ArgumentParser(
        description="Offline adversarial stress test for the hardened semantic cache "
        "(local dev tool; makes no network call and serves no endpoint).",
    )
    parser.add_argument(
        "--set",
        dest="set_path",
        required=True,
        help="path to a JSON adversarial set: a list of {prompt, group}",
    )
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--min-similarity", type=float, default=DEFAULT_MIN_SIMILARITY)
    parser.add_argument(
        "--margin-threshold", type=float, default=DEFAULT_MARGIN_THRESHOLD
    )
    args = parser.parse_args(argv)

    model = load_embedding_model()
    if model is None:
        print(
            "ERROR: embedding model unavailable — the all-MiniLM-L6-v2 artifact "
            "must be warmed/cached locally (run `python -m semantic_cache`).",
            file=sys.stderr,
        )
        return 1

    prompt_set = load_adversarial_set(args.set_path)
    report = run_stress_test(
        prompt_set,
        model=model,
        top_k=args.top_k,
        min_similarity=args.min_similarity,
        margin_threshold=args.margin_threshold,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
