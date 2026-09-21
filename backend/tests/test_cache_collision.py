# Feature: hardened-semantic-cache, Property 2: hardening never increases wrong hits on an adversarial set
"""MANDATORY Property 2 (Collision Resistance) for the hardened semantic cache.

The invariant: on any adversarial prompt set and any valid config, the hardened
cluster-based policy's Wrong_Hit count is <= the naive single-threshold
policy's Wrong_Hit count on the SAME scores, model, and config. A Wrong_Hit is
a policy "hit" whose served top candidate belongs to a different intended-match
group than the query.

Why it holds: a hardened hit requires an extra margin condition beyond the
naive hit on identical scores, so the set of hardened hits is a subset of the
naive hits for the same score vector. Therefore hardened wrong-hits <= naive
wrong-hits always. This test exercises the REAL embedding model for realism
(embedding an adversarial set, computing cosine similarities) while feeding the
SAME per-query score vector to both ``naive_decision`` and ``hardened_decision``
so the comparison is apples-to-apples.

Validates Requirements 3.5, 3.7, 5.4, 5.5, 5.6.

The real ``all-MiniLM-L6-v2`` model is loaded ONCE at module scope; when it is
unavailable the whole module SKIPS (never fails).
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import semantic_cache

# Load the real embedding model ONCE for the module; skip everything if absent.
_MODEL = semantic_cache.load_embedding_model()
pytestmark = pytest.mark.skipif(
    _MODEL is None, reason="embedding model unavailable"
)


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors (embeddings are already unit-length,
    so this is just the dot product; clamp defensively against float noise)."""
    dot = sum(x * y for x, y in zip(a, b))
    return max(-1.0, min(1.0, dot))


# A group base phrase; near-duplicates are formed by deterministic small edits.
_base_phrase = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz ",
    min_size=10,
    max_size=40,
).map(lambda s: s.strip() or "base phrase")

# Deterministic near-duplicate suffixes per group member (small edits that keep
# the prompt semantically near the base).
_NEAR_DUP_SUFFIXES = ["", " please", " now", " quickly", " indeed"]


@st.composite
def _adversarial_sets(draw):
    """Build an Adversarial_Prompt_Set: several groups, each with a few
    near-duplicate prompts. Different groups are distinct base phrases."""
    n_groups = draw(st.integers(min_value=2, max_value=4))
    members_per_group = draw(st.integers(min_value=2, max_value=4))
    items: list[dict] = []
    used_bases: set[str] = set()
    for g in range(n_groups):
        base = draw(_base_phrase)
        # Ensure group bases are distinct so groups are genuinely different.
        while base in used_bases:
            base = base + " x"
        used_bases.add(base)
        group_label = f"g{g}"
        for m in range(members_per_group):
            suffix = _NEAR_DUP_SUFFIXES[m % len(_NEAR_DUP_SUFFIXES)]
            items.append({"prompt": f"{base}{suffix}", "group": group_label})
    return items


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    prompt_set=_adversarial_sets(),
    min_similarity=st.floats(min_value=0.5, max_value=0.9),
    margin_threshold=st.floats(min_value=0.02, max_value=0.2),
)
def test_property_2_hardening_never_increases_wrong_hits(
    prompt_set, min_similarity, margin_threshold
):
    """hardened Wrong_Hit count <= naive Wrong_Hit count for every set + config."""
    # Embed every prompt once (real model).
    embeddings = [semantic_cache.embed(_MODEL, item["prompt"]) for item in prompt_set]
    groups = [item["group"] for item in prompt_set]

    naive_wrong = 0
    hardened_wrong = 0

    for qi in range(len(prompt_set)):
        query_group = groups[qi]
        # Candidate scores against every OTHER prompt (a query never matches
        # itself). Track each candidate's group so we can identify the served
        # top candidate's group.
        scored: list[tuple[float, str]] = []
        for ci in range(len(prompt_set)):
            if ci == qi:
                continue
            sim = _cosine(embeddings[qi], embeddings[ci])
            scored.append((sim, groups[ci]))

        if not scored:
            continue

        # SAME score vector fed to both policies (apples-to-apples).
        scores = [s for s, _ in scored]
        # The served entry is the top-scoring candidate.
        top_score, top_group = max(scored, key=lambda t: t[0])

        naive = semantic_cache.naive_decision(scores, min_similarity=min_similarity)
        hardened, _t, _r, _m = semantic_cache.hardened_decision(
            scores, min_similarity=min_similarity, margin_threshold=margin_threshold
        )

        if naive == "hit" and top_group != query_group:
            naive_wrong += 1
        if hardened == "hit" and top_group != query_group:
            hardened_wrong += 1

    # The core invariant: hardening never increases wrong hits.
    assert hardened_wrong <= naive_wrong, (
        f"hardened wrong-hits ({hardened_wrong}) exceeded naive "
        f"({naive_wrong}) at min_similarity={min_similarity}, "
        f"margin_threshold={margin_threshold}"
    )
