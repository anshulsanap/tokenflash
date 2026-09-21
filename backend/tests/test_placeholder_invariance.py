"""Placeholder invariance through compression.

Covers task 10.2 (Hypothesis property test — Property 3) and task 10.3
(deterministic regression test) of the pre-inference-redaction spec.

The redaction stage substitutes sensitive spans with atomic placeholders of
the form ⟦REDACTED_<CATEGORY>⟧. These placeholders MUST survive the
compressor byte-for-byte: never split, merged, discarded, or relabelled, and
duplicates (from a repeated sensitive value) must all survive because the
compressor performs no content-level dedup.
"""

from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from compressor import compress_prompt_detailed
from redactor import PLACEHOLDER_RE, PLACEHOLDERS

# All placeholder strings the redactor can produce.
_ALL_PLACEHOLDERS: list[str] = list(PLACEHOLDERS.values())

# "Safe" prose words: no ⟦ / ⟧ and none of the compressor's structural
# characters, so they can never accidentally form or corrupt a placeholder.
_SAFE_WORDS = [
    "build",
    "modern",
    "website",
    "selling",
    "handmade",
    "crafts",
    "user",
    "catalogue",
    "payments",
    "deploy",
    "frontend",
    "backend",
    "please",
    "database",
    "authentication",
    "feature",
    "warm",
    "earthy",
    "tone",
]

_safe_word = st.sampled_from(_SAFE_WORDS)
_placeholder = st.sampled_from(_ALL_PLACEHOLDERS)

# A single repeated placeholder appearing 2-5 times, so every generated text
# exercises the "duplicate identical placeholders survive" invariant.
_repeated_run = st.builds(
    lambda ph, n: [ph] * n,
    _placeholder,
    st.integers(min_value=2, max_value=5),
)

# A fragment is either a safe word, a single placeholder, or a run of the same
# placeholder repeated. Interleaving these produces prose with placeholders in
# varied positions and counts, always including at least one repeated case.
_fragment = st.one_of(
    _safe_word.map(lambda w: [w]),
    _placeholder.map(lambda p: [p]),
    _repeated_run,
)


@st.composite
def _redacted_texts(draw):
    """Generate redacted-style text: safe prose interleaved with placeholders,
    guaranteed to contain at least one repeated (>=2 identical) placeholder."""
    fragments = draw(st.lists(_fragment, min_size=1, max_size=8))
    # Force a duplicate case into every example so the duplicate-survival
    # invariant is always exercised (Property 3 / Req 11.3).
    fragments.append(draw(_repeated_run))
    words = [word for fragment in fragments for word in fragment]
    # Shuffle so placeholders land in varied positions (start/middle/end).
    order = draw(st.permutations(list(range(len(words)))))
    words = [words[i] for i in order]
    return " ".join(words)


# Feature: pre-inference-redaction, Property 3: Placeholders are invariant through compression
@settings(max_examples=200)
@given(_redacted_texts())
def test_placeholders_invariant_through_compression(text: str) -> None:
    """Property 3: the multiset of placeholders and their exact characters are
    identical in the compressor output. Validates Req 11.1, 11.2, 11.3, 11.4."""
    result = compress_prompt_detailed(text)
    compressed = result["compressed"]

    before = Counter(PLACEHOLDER_RE.findall(text))
    after = Counter(PLACEHOLDER_RE.findall(compressed))

    # Multiset (count + labels) is preserved — no placeholder dropped, added,
    # or relabelled, and all duplicates survive as duplicates.
    assert after == before

    # Each surviving placeholder is byte-for-byte one of the known placeholder
    # strings (delimiters + label unaltered, never split or merged).
    for placeholder in PLACEHOLDER_RE.findall(compressed):
        assert placeholder in _ALL_PLACEHOLDERS


# Feature: pre-inference-redaction, Property 3: Placeholders are invariant through compression
@settings(max_examples=100)
@given(
    st.lists(_placeholder, min_size=2, max_size=5).filter(
        lambda ps: len(set(ps)) == 1
    ),
    st.lists(_safe_word, min_size=0, max_size=6),
)
def test_repeated_identical_placeholders_all_survive(
    duplicates: list[str], prose: list[str]
) -> None:
    """A repeated sensitive value produces >=2 identical placeholders; ALL of
    them must survive compression (no content-level dedup). Req 11.3."""
    words = list(prose) + list(duplicates)
    text = " ".join(words)
    compressed = compress_prompt_detailed(text)["compressed"]

    placeholder = duplicates[0]
    assert compressed.count(placeholder) == len(duplicates)
