"""
compressor.py — LLMLingua-2-Style Token Compression Engine

Formulates compression as a binary token classification problem:
each token is tagged PRESERVE (1) or DISCARD (0) based on its
bidirectional context score, mirroring the XLM-RoBERTa-large
classifier described in the LLMLingua-2 paper.

Architecture overview
─────────────────────
1. Tokenize the input text (word-level for the stub; sub-word in prod).
2. Score every token using a lightweight feature heuristic that approximates
   a fine-tuned encoder. In production, swap `_score_token` for a real
   forward pass through XLM-RoBERTa-large or a distilled equivalent.
3. Apply a configurable preserve-ratio threshold to decide which tokens survive.
4. Re-join surviving tokens, collapse whitespace, and return the compressed
   string together with compression statistics.

References
──────────
Pan et al. (2024) "LLMLingua-2: Data Distillation for Efficient and Faithful
Task-Agnostic Prompt Compression"
https://arxiv.org/abs/2403.12968
"""

import re
import string
from dataclasses import dataclass, field
from typing import Callable

# ---------------------------------------------------------------------------
# Token dataclass
# ---------------------------------------------------------------------------

@dataclass
class Token:
    text: str
    index: int
    score: float = 0.0          # Preservation probability [0, 1]
    preserve: bool = True


# ---------------------------------------------------------------------------
# Heuristic scoring  (replace with model inference in production)
# ---------------------------------------------------------------------------

# Words that carry almost zero semantic payload — safe to discard
STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could",
    "i", "we", "you", "he", "she", "it", "they",
    "this", "that", "these", "those",
    "and", "but", "or", "nor", "for", "so", "yet",
    "to", "of", "in", "on", "at", "by", "with", "about", "as",
    "if", "then", "than", "because", "while", "although",
    "please", "just", "also", "very", "really", "quite",
})

# JSON structural tokens are almost always critical
JSON_STRUCTURAL: frozenset[str] = frozenset({"{", "}", "[", "]", ":", ","})

# Regex patterns that strongly signal semantic content
_TECH_PATTERN = re.compile(
    r"""
    \b(
        postgresql|mysql|sqlite|mongodb|dynamodb|redis|      # databases
        react|next\.?js|vue|angular|svelte|                  # frameworks
        python|typescript|javascript|rust|go|java|           # languages
        aws|gcp|azure|vercel|netlify|                        # cloud
        auth|payment|stripe|oauth|jwt|api|rest|graphql|      # features
        tailwind|bootstrap|css|html|                         # styling
        \#[0-9a-fA-F]{3,6}|                                  # hex colours
        [a-z0-9_\-]+\.(tsx?|jsx?|py|yaml|json|toml|env)      # file names
    )\b
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _score_token(token_text: str, index: int, total: int) -> float:
    """
    Heuristic preservation score in [0, 1].

    In production this is the softmax output of the classification head
    of a fine-tuned XLM-RoBERTa-large model.  Here we use cheap signal
    proxies that produce realistic compression ratios on structured text.

    Scoring rules (highest wins):
      1.00 — JSON structural characters (always keep)
      1.00 — Numbers and version strings
      0.95 — Matches known-technical-term regex
      0.92 — snake_case / camelCase identifiers (JSON keys, field names)
      0.90 — Double-quote string delimiters (keep so values stay quoted)
      0.85 — Capitalised words (likely proper nouns / identifiers)
      0.62/0.55/0.48/0.35 — graded neutral content words (longer ⇒ higher)
      0.15 — Pure stop words
    """
    t = token_text.strip()

    if not t:
        return 0.0

    # JSON structural tokens are mandatory
    if t in JSON_STRUCTURAL:
        return 1.0

    # Double-quote delimiter — keep it so reconstructed values remain quoted,
    # but score just below structural chars so it never crowds out content.
    if t == '"':
        return 0.90

    # Strip surrounding quotes and trailing/leading punctuation so a word like
    # `authentication,` or `crafts"` is judged on its semantic core, not the
    # punctuation glued to it.
    core = t.strip('"\'').strip(string.punctuation)
    if not core:
        # Nothing but punctuation/quotes left
        return 0.25

    # Numbers and version strings carry high information density
    if re.fullmatch(r"[\d]+(\.\d+)*", core):
        return 1.0

    # snake_case / camelCase identifiers — almost always JSON keys or field
    # names. These are the schema's backbone; preserve them.
    if "_" in core or re.fullmatch(r"[a-z]+[A-Z][a-zA-Z]*", core):
        return 0.92

    # Known technical terms
    if _TECH_PATTERN.search(core):
        return 0.95

    # Capitalised (but not ALL_CAPS shouting) → likely proper noun or identifier
    if core[0].isupper() and not core.isupper():
        return 0.85

    # Stop words — safe to discard
    if core.lower() in STOP_WORDS:
        return 0.15

    # Graded neutral fallback for ordinary content words. Rather than a flat
    # 0.50 (which creates a compression "cliff" — every neutral word shares one
    # score, so the rank threshold keeps all or none of them), we spread scores
    # by informativeness so the threshold can cut partway through. Longer words
    # tend to carry more meaning ("authentication" > "use"); very short words
    # ("use", "need", "best") score lower and are dropped first.
    length = len(core)
    if length <= 3:
        return 0.35
    if length <= 5:
        return 0.48
    if length <= 7:
        return 0.55
    return 0.62


# ---------------------------------------------------------------------------
# Tokenizer  (word-level with JSON-aware quoting)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """
    Split text into word-level tokens.

    Crucially, the *words inside* JSON string values are emitted as individual
    tokens so each one can be scored on its own merits — this is what lets the
    compressor drop stop words ("I", "would", "the") while preserving technical
    terms ("PostgreSQL", "Stripe", "AWS"). Treating a whole quoted value as one
    atomic unit (the old behaviour) meant the entire sentence lived or died on a
    single score, so real content was routinely discarded in favour of the many
    high-scoring structural characters, leaving useless output like "{:,:,:}".

    The double-quote characters themselves are emitted as tokens so the
    reconstruction step can rebuild valid-looking JSON string boundaries.
    Structural JSON characters ({ } [ ] : ,) are isolated.
    """
    parts: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            # Emit the quote as its own token, then tokenize the string
            # *contents* word-by-word until the matching closing quote.
            parts.append('"')
            i += 1
            while i < n and (text[i] != '"' or text[i - 1] == "\\"):
                if text[i].isspace():
                    i += 1
                    continue
                # Grab a run of non-space, non-quote characters as one word.
                j = i
                while j < n and not text[j].isspace() and text[j] != '"':
                    j += 1
                parts.append(text[i:j])
                i = j
            if i < n and text[i] == '"':
                parts.append('"')  # closing quote
                i += 1
        elif c in JSON_STRUCTURAL:
            parts.append(c)
            i += 1
        elif c.isspace():
            i += 1  # skip whitespace outside strings
        else:
            # Bare word outside any quotes
            j = i
            while (
                j < n
                and not text[j].isspace()
                and text[j] not in JSON_STRUCTURAL
                and text[j] != '"'
            ):
                j += 1
            parts.append(text[i:j])
            i = j
    return [p for p in parts if p]


# ---------------------------------------------------------------------------
# Compression statistics dataclass
# ---------------------------------------------------------------------------

@dataclass
class CompressionStats:
    original_tokens: int
    compressed_tokens: int
    ratio: float = field(init=False)
    multiplier: float = field(init=False)

    def __post_init__(self):
        if self.original_tokens > 0:
            self.ratio = round(1 - self.compressed_tokens / self.original_tokens, 4)
        else:
            self.ratio = 0.0
        # Compression multiplier: how many times smaller the prompt became.
        # e.g. 100 → 40 tokens = 2.5×. This is the "2×–5×" headline figure.
        if self.compressed_tokens > 0:
            self.multiplier = round(self.original_tokens / self.compressed_tokens, 2)
        else:
            self.multiplier = 1.0

    def as_dict(self) -> dict:
        return {
            "original_tokens": self.original_tokens,
            "compressed_tokens": self.compressed_tokens,
            "ratio": self.ratio,
            "multiplier": self.multiplier,
        }


# ---------------------------------------------------------------------------
# Core compression function
# ---------------------------------------------------------------------------

def compress_prompt(
    text: str,
    preserve_ratio: float = 0.5,
    score_fn: Callable[[str, int, int], float] | None = None,
) -> tuple[str, dict]:
    """
    Compress *text* by discarding low-value tokens.

    Args:
        text:           The raw prompt / JSON requirements string.
        preserve_ratio: Fraction of tokens to keep (0 < ratio ≤ 1).
                        Lower = more aggressive compression.
                        Default 0.5 ≈ 2× compression (40–60% token savings).
        score_fn:       Optional override for the per-token scorer.
                        Signature: (token_text, index, total) -> float

    Returns:
        A tuple of (compressed_string, stats_dict).

    Example:
        >>> compressed, stats = compress_prompt('{"app_type": "e-commerce site"}')
        >>> print(stats)
        {'original_tokens': 5, 'compressed_tokens': 3, 'ratio': 0.4}
    """
    if not text or not text.strip():
        return text, CompressionStats(0, 0).as_dict()

    scorer = score_fn or _score_token
    tokens = _score_and_tag_tokens(text, preserve_ratio, scorer)
    total = len(tokens)

    if total == 0:
        return text, CompressionStats(0, 0).as_dict()

    kept = [tk.text for tk in tokens if tk.preserve]
    compressed = _reconstruct(kept)

    stats = CompressionStats(
        original_tokens=total,
        compressed_tokens=len(kept),
    )
    return compressed, stats.as_dict()


def _score_and_tag_tokens(
    text: str,
    preserve_ratio: float,
    scorer: Callable[[str, int, int], float],
) -> list[Token]:
    """
    Tokenize *text*, score every token, apply the JSON-key position boost, and
    tag each token's ``preserve`` flag against the rank threshold. Returns the
    full list of Token objects (in original order) so callers can either
    reconstruct the compressed string or inspect the per-token kept/dropped
    decision for a before/after diff view.
    """
    raw_tokens = _tokenize(text)
    total = len(raw_tokens)
    if total == 0:
        return []

    tokens = [
        Token(text=t, index=i, score=scorer(t, i, total))
        for i, t in enumerate(raw_tokens)
    ]

    # Position-aware JSON key boost: any word token sitting immediately before
    # a ':' (ignoring an intervening closing quote) is a JSON key and is the
    # schema's backbone — force it to a preserve-worthy score. This is far more
    # reliable than guessing keys from spelling, and stops single-word keys like
    # "database" or "auth" from being compressed away into empty '"":"..."'.
    for idx, tk in enumerate(tokens):
        if tk.text == ":":
            k = idx - 1
            if k >= 0 and tokens[k].text == '"':
                k -= 1
            if k >= 0 and tokens[k].text not in JSON_STRUCTURAL and tokens[k].text != '"':
                tokens[k].score = max(tokens[k].score, 0.93)

    # Determine the score threshold that keeps ~preserve_ratio of tokens.
    sorted_scores = sorted((tk.score for tk in tokens), reverse=True)
    keep_count = max(1, int(total * preserve_ratio))
    threshold = sorted_scores[min(keep_count - 1, len(sorted_scores) - 1)]

    # Tag each token — always keep structural JSON tokens and string-delimiter
    # quotes regardless of threshold, so the JSON skeleton never breaks (a
    # dropped quote turns valid '"key":"val"' into mangled 'key:val').
    for tk in tokens:
        tk.preserve = (
            (tk.score >= threshold)
            or (tk.text in JSON_STRUCTURAL)
            or (tk.text == '"')
        )

    return tokens


def _reconstruct(kept: list[str]) -> str:
    """Re-join preserved tokens and tidy up JSON/prose spacing artefacts."""
    compressed = " ".join(kept)

    #   1. Remove whitespace around structural chars:  "{ " → "{"
    compressed = re.sub(r"\s*([{}[\]:,])\s*", r"\1", compressed)
    #   2. Remove the space between an opening quote and its first word, and
    #      between the last word and a closing quote:  '" foo bar "' → '"foo bar"'
    compressed = re.sub(r'"\s+', '"', compressed)
    compressed = re.sub(r'\s+"', '"', compressed)
    #   3. Collapse any quoted value whose words were all discarded ("" → "")
    compressed = re.sub(r'""', '""', compressed)
    #   4. Collapse any residual multiple spaces inside values
    compressed = re.sub(r"\s{2,}", " ", compressed)
    #   5. Collapse runs of orphaned commas left behind when a sequence of
    #      low-value words was dropped (e.g. "Features:,,,,," → "Features:").
    compressed = re.sub(r",\s*(?:,\s*)+", ",", compressed)   # ",,,," → ","
    compressed = re.sub(r"([:{[])\s*,+", r"\1", compressed)  # ":," → ":"
    compressed = re.sub(r",+\s*([}\]])", r"\1", compressed)  # ",}" → "}"
    compressed = re.sub(r",\s*$", "", compressed)            # trailing comma
    #   5b. A comma sitting right before a section label (a Word followed by
    #       ':') is a leftover list separator from a previous value whose tail
    #       words were dropped — e.g. "required,Deployment:" → "required Deployment:".
    #       Turn it into a space so the label starts cleanly.
    compressed = re.sub(r",\s*(?=[A-Za-z][\w.\-]*\s*:)", " ", compressed)
    #   6. Strip orphaned colons whose label word was dropped, so prose like
    #      "Frontend: X" that lost "Frontend" doesn't leave a leading ":".
    compressed = re.sub(r"(^|[\s,{[])\s*:+\s*", r"\1", compressed)  # ":X" / ", :"
    compressed = re.sub(r":\s*(?=[:,])", "", compressed)     # ":: " / ":," → drop
    compressed = re.sub(r"\s{2,}", " ", compressed)
    return compressed.strip(" :,")


def compress_prompt_detailed(
    text: str,
    preserve_ratio: float = 0.5,
    score_fn: Callable[[str, int, int], float] | None = None,
) -> dict:
    """
    Like :func:`compress_prompt`, but also returns the per-token kept/dropped
    breakdown so the frontend can render a before/after diff (original text
    with discarded tokens visibly struck through).

    Returns a dict:
        {
            "compressed":  "<compressed string>",
            "tokens":      [{"text": str, "kept": bool, "score": float}, ...],
            "stats":       { original_tokens, compressed_tokens, ratio, multiplier },
        }
    """
    if not text or not text.strip():
        return {
            "compressed": text,
            "tokens": [],
            "stats": CompressionStats(0, 0).as_dict(),
        }

    scorer = score_fn or _score_token
    tokens = _score_and_tag_tokens(text, preserve_ratio, scorer)
    total = len(tokens)
    if total == 0:
        return {
            "compressed": text,
            "tokens": [],
            "stats": CompressionStats(0, 0).as_dict(),
        }

    kept = [tk.text for tk in tokens if tk.preserve]
    compressed = _reconstruct(kept)
    stats = CompressionStats(original_tokens=total, compressed_tokens=len(kept))

    return {
        "compressed": compressed,
        "tokens": [
            {"text": tk.text, "kept": tk.preserve, "score": round(tk.score, 2)}
            for tk in tokens
        ],
        "stats": stats.as_dict(),
    }


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sample = (
        '{"app_type": "I would really like to build a modern e-commerce website '
        'for selling handmade crafts", '
        '"color_palette": "Please use a warm earthy tone with browns and greens", '
        '"database": "I think PostgreSQL would be the best choice for this", '
        '"features": "I need user authentication, a product catalogue, and Stripe payments", '
        '"deployment": "Please deploy it to AWS using Vercel for the frontend"}'
    )

    print("=== LLMLingua-2 Style Compression ===\n")
    print(f"Original ({len(sample)} chars):\n{sample}\n")

    compressed, stats = compress_prompt(sample, preserve_ratio=0.55)

    print(f"Compressed ({len(compressed)} chars):\n{compressed}\n")
    print(f"Stats: {stats}")
