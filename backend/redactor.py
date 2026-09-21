"""
redactor.py — Pre-inference redaction core primitives

This module defines the foundational, dependency-free primitives for the
pre-inference redaction stage:

  * ``Span``            — an immutable (start, end, category) region into the
                          ORIGINAL text. It never stores the raw value.
  * ``Detector``        — a ``typing.Protocol`` (interface) every detector
                          implements: a ``category`` attribute plus a pure
                          ``detect(text) -> list[Span]`` method.
  * ``RedactionResult`` — the immutable result of a ``redact()`` call.
  * The Redaction_Placeholder format (Option A: ``⟦REDACTED_<CATEGORY>⟧``)
    together with the category ↔ placeholder mapping and helpers.
  * ``verify_placeholders`` — a post-compression invariance check.

Design references: "Detector interface", "RedactionResult dataclass",
"Placeholder format decision", and "Placeholder preservation verification"
sections of the pre-inference-redaction design document.

Nothing in this module performs any I/O or network call.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

# The seven redaction categories. `custom_term` and `person` join the five
# built-in structured-secret categories. Kept as a tuple so it is an immutable
# single source of truth for the placeholder mapping below.
CATEGORIES: tuple[str, ...] = (
    "ssn",
    "credit_card",
    "email",
    "phone",
    "api_key",
    "custom_term",
    "person",
)


# ---------------------------------------------------------------------------
# Span (Req 1.3, 1.4, 6.3, 7.3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Span:
    """A detected sensitive region into the ORIGINAL text.

    Carries only offsets and a category — never the raw value — so a Span (or
    any structure built from it) cannot leak a secret if logged or serialized.
    """

    start: int          # inclusive char offset into the ORIGINAL text
    end: int            # exclusive char offset
    category: str       # one of CATEGORIES
    # NOTE: no raw value is ever stored on a Span.


# ---------------------------------------------------------------------------
# Detector interface (Protocol, not a dataclass)
# ---------------------------------------------------------------------------

@runtime_checkable
class Detector(Protocol):
    """Interface implemented by every detector.

    ``detect`` MUST be pure: it depends only on its ``text`` argument (and any
    immutable snapshot captured at construction time) and performs no I/O and
    no network call, so it is deterministic for a given input.
    """

    category: str

    def detect(self, text: str) -> list[Span]:
        """Return all spans this detector matches in ``text``."""
        ...


# ---------------------------------------------------------------------------
# Placeholder format (Option A) — ⟦REDACTED_<CATEGORY>⟧  (Req 1.3, 11.4)
# ---------------------------------------------------------------------------
#
# Delimiters U+27E6 (⟦) and U+27E7 (⟧) contain none of the compressor's
# structural characters { } [ ] : , the double-quote ", or any whitespace, so
# the compressor tokenizer emits a whole placeholder as one bare word and never
# rewrites it. The label uses uppercased category text joined by underscores.

_LEFT = "\u27e6"    # ⟦
_RIGHT = "\u27e7"    # ⟧

# Single source of truth: category -> placeholder string.
PLACEHOLDERS: Mapping[str, str] = MappingProxyType(
    {category: f"{_LEFT}REDACTED_{category.upper()}{_RIGHT}" for category in CATEGORIES}
)

# Matches a single placeholder token. The label is one or more uppercase
# letters/underscores between the angle-bracket delimiters.
PLACEHOLDER_RE = re.compile(r"\u27e6REDACTED_[A-Z_]+\u27e7")

# Reverse lookup: placeholder string -> category. Immutable.
_PLACEHOLDER_TO_CATEGORY: Mapping[str, str] = MappingProxyType(
    {placeholder: category for category, placeholder in PLACEHOLDERS.items()}
)


def placeholder_for(category: str) -> str:
    """Return the Redaction_Placeholder string for ``category``.

    Raises ``KeyError`` for an unknown category so callers cannot silently
    build an unlabelled placeholder.
    """
    return PLACEHOLDERS[category]


def category_for(placeholder: str) -> str | None:
    """Parse a placeholder token back to its category.

    Returns the category for a known placeholder, or ``None`` if the token is
    not a recognised placeholder.
    """
    return _PLACEHOLDER_TO_CATEGORY.get(placeholder)


# ---------------------------------------------------------------------------
# RedactionResult dataclass (Req 1.3, 1.4, 6.3, 7.3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RedactionResult:
    """Immutable result of a redaction pass.

    ``redact()`` accumulates its work into local mutable structures (a list of
    spans, a dict of counts) while splicing the text, then freezes them into
    this immutable result exactly once at return time: ``redactions`` becomes a
    tuple and ``category_counts`` is wrapped in a read-only mapping, so the
    frozen guarantee is real and the object cannot leak or be mutated after
    construction.

    ``redactions`` carries only category + offsets — never a raw value.
    """

    redacted_text: str                      # summary with placeholders substituted
    redactions: tuple[Span, ...]            # immutable; category + offsets, NO raw value
    category_counts: Mapping[str, int]      # read-only mapping, per-category count
    chars_redacted: int                     # sum of original span lengths
    latency_ms: float                       # non-negative, stage start→completion
    ok: bool = True                         # False on redaction failure (Req 1.7)


# ---------------------------------------------------------------------------
# Placeholder preservation verification (Req 11.1, 11.3, 11.5)
# ---------------------------------------------------------------------------

def verify_placeholders(redacted_summary: str, compressed_output: str) -> bool:
    """Return True iff placeholders survived compression unchanged.

    Extracts every placeholder token from both strings via ``PLACEHOLDER_RE``
    and compares them as a MULTISET (count + labels) using ``Counter``. This is
    the correct check because the compressor performs no content-level dedup:
    a repeated sensitive value yields >=2 identical placeholders, and each is
    preserved independently, so duplicates must survive as duplicates.

    Any mismatch (a dropped, added, split, or relabelled placeholder) returns
    False; the caller treats that as a redaction failure (Req 1.7 / 11.5).
    """
    before = Counter(PLACEHOLDER_RE.findall(redacted_summary))
    after = Counter(PLACEHOLDER_RE.findall(compressed_output))
    return before == after

# ===========================================================================
# Built-in regex detectors (Req 2)
# ===========================================================================
#
# Five structured-secret detectors, one per built-in structured category:
# ``ssn``, ``credit_card``, ``email``, ``phone``, and ``api_key``. Each
# implements the ``Detector`` Protocol (a ``category`` attribute plus a pure
# ``detect(text) -> list[Span]``) and returns ALL non-overlapping matches in a
# single pass (Req 2.7). Detection uses only local regular expressions — no
# network, no I/O (Req 2.6) — which is inherent to regex.
#
# All patterns are compiled ONCE at module import (the module-level constants
# below), never per call, so ``detect`` merely iterates precompiled matches.
#
# The ``custom_term`` and ``person`` categories are handled by other detectors
# (custom-term store and NER) and are intentionally NOT implemented here.
# Cross-category overlap resolution / precedence is handled centrally by
# ``redact()`` (task 8); these detectors only surface their own matches.


# --- SSN (Req 2.1) ---------------------------------------------------------
# Conservative on purpose: match only the clearly-delimited US SSN forms so we
# do not collide with unrelated 9-digit runs (a bare \d{9} produces too many
# false positives). Two accepted forms: hyphen-delimited and single-space
# delimited, each anchored on word boundaries.
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b\d{3} \d{2} \d{4}\b")

# --- Credit card (Req 2.2) -------------------------------------------------
# Find CANDIDATE runs of 13-19 digits allowing single spaces or hyphens
# between digits, then STRICTLY validate each candidate with the Luhn
# algorithm below. Only Luhn-passing candidates become spans. The candidate
# regex is deliberately permissive on grouping; the Span covers the full
# matched substring INCLUDING its separators, while Luhn runs on digits only.
_CC_CANDIDATE_RE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")

# --- Email (Req 2.3) -------------------------------------------------------
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# --- Phone (Req 2.4) -------------------------------------------------------
# Common US/international formats: an optional leading '+', then digits mixed
# with spaces, dashes, dots, and parentheses, ~7+ digits total. Bounded to
# avoid swallowing arbitrarily long digit strings.
_PHONE_RE = re.compile(r"\+?\d(?:[\d\s().-]{5,}\d)")

# --- API keys / tokens (Req 2.5) -------------------------------------------
# Alternation of the four explicitly-called-out forms; each matches
# independently:
#   * sk-…      OpenAI-style secret keys, incl. sk-proj-… (16+ key chars after
#               the prefix, allowing internal '-' segments and underscores)
#   * AKIA…     AWS access key IDs: AKIA + 16 uppercase alphanumerics
#   * ghp_…     GitHub personal access tokens: ghp_ + 36 alphanumerics
#   * Bearer …  Authorization bearer tokens: Bearer + whitespace + 8+ token
#               chars from [A-Za-z0-9._-]
_API_KEY_RE = re.compile(
    r"sk-[A-Za-z0-9][A-Za-z0-9_-]{15,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|ghp_[A-Za-z0-9]{36}"
    r"|Bearer\s+[A-Za-z0-9._-]{8,}"
)


def _luhn_ok(digits: str) -> bool:
    """Return True iff ``digits`` (a string of decimal digits) passes the Luhn
    checksum: right-to-left, double every second digit, subtract 9 when the
    doubled value exceeds 9, and require the total sum modulo 10 to be 0.
    """
    total = 0
    # Enumerate from the rightmost digit (index 0 = check digit).
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48  # fast int(char) for known-digit input
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


class SsnDetector:
    """Detect US Social Security Numbers in delimited form (Req 2.1)."""

    category: str = "ssn"

    def detect(self, text: str) -> list[Span]:
        return [Span(m.start(), m.end(), self.category) for m in _SSN_RE.finditer(text)]


class CreditCardDetector:
    """Detect credit card numbers, Luhn-validated to cut false positives (Req 2.2).

    Finds candidate 13-19 digit runs (with optional single space/hyphen
    separators), strips separators for the Luhn check, and emits a Span
    covering the FULL matched substring only when Luhn passes.
    """

    category: str = "credit_card"

    def detect(self, text: str) -> list[Span]:
        spans: list[Span] = []
        for match in _CC_CANDIDATE_RE.finditer(text):
            candidate = match.group()
            digits = candidate.replace(" ", "").replace("-", "")
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                spans.append(Span(match.start(), match.end(), self.category))
        return spans


class EmailDetector:
    """Detect email addresses (Req 2.3)."""

    category: str = "email"

    def detect(self, text: str) -> list[Span]:
        return [Span(m.start(), m.end(), self.category) for m in _EMAIL_RE.finditer(text)]


class PhoneDetector:
    """Detect common US/international phone number formats (Req 2.4)."""

    category: str = "phone"

    def detect(self, text: str) -> list[Span]:
        return [Span(m.start(), m.end(), self.category) for m in _PHONE_RE.finditer(text)]


class ApiKeyDetector:
    """Detect API keys / tokens: sk-…, AKIA…, ghp_…, and Bearer … (Req 2.5)."""

    category: str = "api_key"

    def detect(self, text: str) -> list[Span]:
        return [Span(m.start(), m.end(), self.category) for m in _API_KEY_RE.finditer(text)]


# Ordered collection of the built-in regex detector instances. Ordered by the
# design's cross-category precedence (ssn, credit_card, api_key, email, phone)
# so later orchestration (task 8) can iterate them in a natural order; the
# actual overlap resolution happens there, not here.
BUILTIN_REGEX_DETECTORS: tuple[Detector, ...] = (
    SsnDetector(),
    CreditCardDetector(),
    ApiKeyDetector(),
    EmailDetector(),
    PhoneDetector(),
)

# ===========================================================================
# NER detector + local model load helper (Req 3)
# ===========================================================================
#
# The NER detector surfaces person/entity spans under the ``person`` category
# using a local spaCy model. It is deliberately decoupled from model loading:
# the constructor takes an already-loaded ``nlp`` object (or ``None``), so the
# detector's ``detect`` stays a pure function of its input text and this module
# imports cleanly even where spaCy is not installed.
#
# ``load_ner_model()`` is the single place that touches spaCy. spaCy is imported
# LAZILY inside that function (not at module import) for two reasons:
#   1. Graceful degradation — redactor.py must import even in an environment
#      without spaCy, so unit tests that pass ``nlp=None`` or a fake nlp never
#      require the dependency.
#   2. Fail-closed loading — any load failure (missing artifact, import error,
#      incompatible model, ...) is caught, logged as a WARNING, and turned into
#      a ``None`` return so the server never crashes and simply degrades to
#      regex + custom-term detection (Req 3.5). The caller stores the result
#      and sets ``redaction_state.ner_available`` accordingly.
#
# NER runs entirely on-device against a local artifact; no network call is made
# at load time or at detection time (Req 3.3). ``NerDetector`` is intentionally
# NOT part of ``BUILTIN_REGEX_DETECTORS`` (that tuple is regex-only); the
# generate-phase orchestration wires NER in separately.

import logging

logger = logging.getLogger(__name__)

# spaCy entity labels we treat as ``person``-category redactions (Req 3.1).
_NER_LABELS: frozenset[str] = frozenset({"PERSON", "ORG", "GPE"})

# ---------------------------------------------------------------------------
# Tech-stack allowlist — suppress NER false positives
# ---------------------------------------------------------------------------
#
# spaCy's small NER model routinely mislabels common technology names (HTML,
# CSS, JavaScript, React, Vue, Angular, Node.js, Django, ...) as PERSON / ORG /
# GPE entities. Left unchecked, ``NerDetector`` would emit a ``person`` span for
# each and scrub a legitimate tech mention to ⟦REDACTED_PERSON⟧, corrupting the
# very requirements a user is trying to describe.
#
# ``TECH_STACK_ALLOWLIST`` is the human-readable source of truth (raw casing).
# ``_TECH_STACK_LOWER`` is a ``frozenset`` of the same names lowercased, so the
# per-entity gate in ``NerDetector.detect`` is an O(1) case-insensitive lookup.
# This ONLY gates the ``person``-category NER span emission — the regex
# detectors and the custom-term detector are never consulted here and are
# entirely unaffected. The gate is applied PER-ENTITY, so a genuine PERSON that
# merely co-occurs with a tech name in the same prompt is still redacted.
TECH_STACK_ALLOWLIST: frozenset[str] = frozenset({
    "HTML", "HTML5", "CSS", "CSS3", "JavaScript", "JS", "TypeScript", "TS",
    "React", "Vue", "Vue.js", "Angular", "Node", "Node.js", "Express",
    "Express.js", "Django", "Flask", "FastAPI", "Ruby on Rails", "Rails",
    "Python", "Java", "SQL", "MySQL", "MongoDB", "PostgreSQL", "Postgres",
    "Redis", "SQLite", "Tailwind", "Tailwind CSS", "Bootstrap", "Next.js",
    "Nuxt", "Svelte", "jQuery", "Webpack", "Vite", "Babel", "GraphQL", "REST",
    "Docker", "Kubernetes", "Go", "Golang", "Rust", "PHP", "Laravel", "Spring",
    "Kotlin", "Swift", "C++", "C#", "Sass", "SCSS", "Redux",
})

# Lowercased view for O(1) case-insensitive membership tests.
_TECH_STACK_LOWER: frozenset[str] = frozenset(
    name.lower() for name in TECH_STACK_ALLOWLIST
)

# Separators the model uses when it captures a JOINED tech list as one entity
# (e.g. "React/Vue/Angular", "HTML, CSS, JavaScript", "React or Vue", "Node & Express").
# Used by _is_allowlisted_tech candidate 3. Word separators "or"/"and" are matched
# only as whole words (surrounded by whitespace) so they never split inside a term.
_TECH_LIST_SPLIT_RE = re.compile(r"\s*(?:[/,|+&]|\bor\b|\band\b)\s*", re.IGNORECASE)


def _is_allowlisted_tech(entity_text: str) -> bool:
    """Return True iff ``entity_text`` names an allowlisted tech-stack term.

    NER sometimes captures an entity with surrounding whitespace or a trailing
    separator (e.g. ``"React"``, ``"Node.js"``, ``"CSS,"``, ``"Angular)"``), so
    we normalize before comparing: strip surrounding whitespace, then strip a
    conservative set of trailing/leading punctuation the tokenizer commonly
    leaves attached, WITHOUT stripping a trailing ``.`` from names that legitimately
    end in one at the boundary (we test both the stripped and the dot-preserving
    forms). Matching is case-insensitive via the lowercased allowlist.

    Pure: no I/O, depends only on its argument and the module-level allowlist.
    """
    stripped = entity_text.strip()
    if not stripped:
        return False

    # Candidate 1: as-is (handles exact tokens like "Node.js", "C++", "C#").
    if stripped.lower() in _TECH_STACK_LOWER:
        return True

    # Candidate 2: trailing separator punctuation removed (", " / "." / ")"),
    # e.g. NER captured "CSS," or "Angular)" — but keep names like "Node.js"
    # intact via candidate 1 above, which already matched before we get here.
    trimmed = stripped.strip(" \t\r\n.,;:!?()[]{}/|\\\"'")
    if trimmed and trimmed.lower() in _TECH_STACK_LOWER:
        return True

    # Candidate 3: a SLASH/COMMA/etc-JOINED tech list captured as ONE entity.
    # The real spaCy model frequently tokenizes "React/Vue/Angular" or
    # "HTML/CSS/JavaScript" (and comma/pipe/plus/"or"/"and"-joined variants) as a
    # SINGLE entity, so neither candidate above matches and the whole list would
    # otherwise be scrubbed to ⟦REDACTED_PERSON⟧. Split on the common list
    # separators and treat the entity as allowlisted ONLY when EVERY non-empty
    # component is itself an allowlisted tech term. This is conservative and
    # safe: "React/Vue/Angular" is skipped (all components tech), while a mixed
    # entity like "Angular/John Doe" — where a component is NOT a tech term — is
    # NOT allowlisted and is still redacted whole. Requires >=2 components so a
    # lone token still only matches via candidates 1/2 above.
    components = [c for c in _TECH_LIST_SPLIT_RE.split(stripped) if c.strip()]
    if len(components) >= 2 and all(
        c.strip().lower() in _TECH_STACK_LOWER for c in components
    ):
        return True

    return False


class NerDetector:
    """Detect person/entity spans via a local spaCy NER model (Req 3.1, 3.5).

    Constructed with an already-loaded spaCy ``nlp`` object, or ``None`` when
    the model is unavailable. When ``nlp`` is ``None`` the detector emits NO
    ``person`` spans (the graceful fallback of Req 3.5); otherwise it maps each
    ``PERSON``/``ORG``/``GPE`` entity to a ``person`` Span carrying only the
    entity's character offsets — never the raw text. Entities whose text is an
    allowlisted tech-stack term (see ``TECH_STACK_ALLOWLIST``) are skipped so
    common technology names (HTML, CSS, React, Node.js, ...) are not mistaken
    for people. ``detect`` is pure with respect to its input and makes no
    network call (Req 3.3).
    """

    category: str = "person"

    def __init__(self, nlp=None):
        self._nlp = nlp

    def detect(self, text: str) -> list[Span]:
        if self._nlp is None:
            return []  # fallback: emit no person spans until a model is loaded
        doc = self._nlp(text)
        # Emit a ``person`` span for each PERSON/ORG/GPE entity EXCEPT those
        # whose text is an allowlisted tech-stack term (case-insensitive). The
        # allowlist check is per-entity, so a genuine name that co-occurs with a
        # tech term in the same prompt is still redacted. The entity text is read
        # from ``ent.text`` (as real spaCy provides); if absent it is derived from
        # the entity's offsets into the original text, so the gate works either way.
        spans: list[Span] = []
        for ent in doc.ents:
            if ent.label_ not in _NER_LABELS:
                continue
            entity_text = getattr(ent, "text", None)
            if entity_text is None:
                entity_text = text[ent.start_char : ent.end_char]
            if _is_allowlisted_tech(entity_text):
                continue
            spans.append(Span(ent.start_char, ent.end_char, self.category))
        return spans


def load_ner_model():
    """Load the local spaCy NER model, or return ``None`` on ANY failure.

    Loads ``en_core_web_sm`` with ONLY the NER pipe enabled to minimize
    per-request CPU latency. ``spacy.load(..., enable=["ner"])`` enables just
    the listed pipe and disables the rest while still resolving NER's upstream
    dependencies (e.g. ``tok2vec``) correctly. If this spaCy build rejects the
    ``enable=`` form, we fall back to a ``disable=`` load that keeps ``tok2vec``
    (NER depends on it) and only drops pipes NER does not need.

    NEVER raises: any exception (missing artifact, import error, incompatible
    model, ...) is caught, logged as a WARNING, and reported as a ``None``
    return so a load failure can never crash the server or a user request. The
    caller stores the result and sets ``ner_available`` accordingly (Req 3.5).

    spaCy is imported lazily here so redactor.py imports cleanly without spaCy.
    """
    try:
        import spacy  # lazy import — see module note above

        try:
            # Preferred: enable only the NER pipe (handles pipe dependencies).
            nlp = spacy.load("en_core_web_sm", enable=["ner"])
        except Exception:
            # Robustness fallback for spaCy builds that reject enable=[...].
            # Keep tok2vec (NER depends on it); drop only unneeded pipes.
            nlp = spacy.load(
                "en_core_web_sm",
                disable=["parser", "tagger", "lemmatizer", "attribute_ruler"],
            )
        return nlp
    except Exception as err:  # noqa: BLE001 — degrade on ANY failure
        logger.warning(
            "NER model failed to load; degrading to regex-only detection: %s", err
        )
        return None

# ===========================================================================
# redact() orchestration (Req 1, 2.8, 3.2, 9) — tasks 8.1, 8.2
# ===========================================================================
#
# This section ties the detectors together into the pre-inference redaction
# stage. It has two parts:
#
#   * ``resolve_overlaps`` (task 8.1) — a PURE function that filters a list of
#     candidate ``Span``s down to a NON-overlapping set under a fixed
#     cross-category precedence, so each character is redacted at most once and
#     never under a lower-precedence category. No I/O, no side effects.
#
#   * ``redact`` (task 8.2) — the orchestration entry point. It collects spans
#     from the active detectors, resolves overlaps, splices placeholders in
#     right-to-left order (so earlier offsets stay valid), fires the telemetry
#     side-effects (audit log + session counts) exactly once per replacement,
#     and returns a frozen ``RedactionResult`` with the benchmark values.

import time

# ---------------------------------------------------------------------------
# Cross-category precedence (Req 2.8, 3.2)
# ---------------------------------------------------------------------------
#
# Fixed precedence order, HIGHEST first. When two candidate spans overlap
# (share any character range) the higher-precedence category wins and the
# lower-precedence span is dropped. This is the single source of truth for the
# overlap resolver below; index in the tuple == rank (0 = highest precedence).
#
# In particular ``person`` (NER) is LAST, so a ``person`` span can never
# survive over characters already claimed by a regex or custom-term detector
# (Req 3.2 — no duplicate ``person`` redaction over another detector's range).
PRECEDENCE: tuple[str, ...] = (
    "ssn",
    "credit_card",
    "api_key",
    "email",
    "phone",
    "custom_term",
    "person",
)

# category -> rank (lower number == higher precedence). Built once.
_PRECEDENCE_RANK: Mapping[str, int] = MappingProxyType(
    {category: index for index, category in enumerate(PRECEDENCE)}
)


def precedence_rank(category: str) -> int:
    """Return the precedence rank of ``category`` (0 = highest precedence).

    An unknown category ranks AFTER every known one (a large sentinel), so a
    stray category can never silently outrank a real one.
    """
    return _PRECEDENCE_RANK.get(category, len(PRECEDENCE))


def _spans_overlap(a: Span, b: Span) -> bool:
    """Return True iff spans ``a`` and ``b`` share at least one character.

    Spans are half-open ``[start, end)``; they overlap iff each starts before
    the other ends. Zero-length spans (start == end) cover no character and so
    never overlap anything.
    """
    if a.start >= a.end or b.start >= b.end:
        return False
    return a.start < b.end and b.start < a.end


def resolve_overlaps(spans: list[Span]) -> list[Span]:
    """Filter overlapping candidate spans to a non-overlapping set (task 8.1).

    Given all candidate spans from every detector, return the subset that
    survives cross-category precedence, such that:

      * the result spans are pairwise NON-overlapping — every original
        character is covered by at most one surviving span;
      * each surviving span is one of the inputs (offsets are never trimmed or
        merged — a lower-precedence span that overlaps a higher-precedence one
        is dropped whole, not split);
      * for any character covered by >=1 candidate, if it is covered in the
        result it is under the HIGHEST-precedence category among all candidates
        covering it (Req 2.8, 3.2).

    Selection order / tie-breaks (documented):
      1. Higher precedence first (``ssn`` > ... > ``person``).
      2. Same precedence: EARLIER start first.
      3. Same precedence and start: LONGER span first (wider end).
      4. Deterministic final tiebreak on (start, end, category) so equal
         candidates order stably.

    Spans are then admitted greedily in that order, each accepted only if it
    does not overlap any already-accepted span. Because higher precedence is
    considered first, a lower-precedence span overlapping an accepted higher
    one is rejected — exactly the required behavior. Zero-length spans cover no
    character and are dropped. The returned list is sorted by ascending start
    (natural reading order) for a stable, easy-to-consume result.

    Pure: no I/O, no mutation of the input list's elements.
    """
    # Drop zero-length / malformed spans up front; they redact nothing.
    candidates = [s for s in spans if s.end > s.start]

    # Order by the selection priority above. Note key #3 uses -end so a longer
    # span (larger end, same start) sorts before a shorter one.
    candidates.sort(
        key=lambda s: (
            precedence_rank(s.category),  # 1: higher precedence first
            s.start,                      # 2: earlier start first
            -s.end,                       # 3: longer span first
            s.category,                   # 4: stable final tiebreak
        )
    )

    accepted: list[Span] = []
    for span in candidates:
        if any(_spans_overlap(span, kept) for kept in accepted):
            continue  # overlaps a higher/earlier-priority accepted span → drop
        accepted.append(span)

    # Return in natural ascending order (by start, then end).
    accepted.sort(key=lambda s: (s.start, s.end))
    return accepted


def redact(
    text: str,
    session_id: str,
    *,
    detectors: "tuple[Detector, ...] | list[Detector] | None" = None,
    audit=None,
    state=None,
) -> RedactionResult:
    """Run the pre-inference redaction stage over ``text`` (task 8.2).

    Parameters
    ----------
    text:
        The original requirements summary to redact.
    session_id:
        The chat session id used for audit entries and cumulative counts.
    detectors:
        The active detector set. When ``None`` this defaults to
        ``BUILTIN_REGEX_DETECTORS`` only — the NER and custom-term detectors
        need runtime objects (a loaded spaCy model, a live term store) and are
        injected by the caller (main.py / tests) when available.
    audit:
        An ``AuditLog``-like object exposing
        ``append(category, session_id, *, length, placeholder)``. When ``None``
        the audit side-effect is skipped (so unit/property tests can run
        without touching a file). When provided it fires exactly once per
        actual replacement.
    state:
        A ``RedactionState``-like object exposing ``record(session_id,
        category)``. When ``None`` the count side-effect is skipped. When
        provided it fires exactly once per actual replacement.

    Behavior
    --------
    1. ``start = time.perf_counter()`` (Req 9.1).
    2. Collect spans from every active detector. If ANY detector raises, FAIL
       CLOSED (Req 1.7): return ``RedactionResult`` with ``ok=False`` and NO
       partial/unredacted text — ``redacted_text=""`` and empty redactions — so
       the caller (task 11) blocks compression and emits ``redaction_failure``.
       Choosing "" (rather than the original text) guarantees the unredacted
       summary can never leak downstream on a detector error.
    3. ``resolve_overlaps`` → the final non-overlapping span set.
    4. Splice placeholders RIGHT-TO-LEFT (spans sorted by start descending) so
       replacing a later span never invalidates an earlier span's offsets;
       non-span characters are copied byte-for-byte (Req 1.4).
    5. For EACH spliced span fire ``audit.append(...)`` and ``state.record(...)``
       once — telemetry equals reality (k replacements => k appends => k
       recorded increments).
    6. Compute per-category counts, ``chars_redacted`` (sum of span lengths),
       and ``latency_ms`` (Req 9.1-9.3).
    7. Return a frozen ``RedactionResult`` (redactions in natural ascending
       order, read-only category-count mapping, ``ok=True``).
    8. No spans → return the ORIGINAL text unchanged, ``ok=True``, empty
       redactions, zero counts (Req 1.6, 2.9).

    Never makes a network call.
    """
    start = time.perf_counter()

    active = tuple(detectors) if detectors is not None else BUILTIN_REGEX_DETECTORS

    # --- 2. Collect candidate spans; fail closed on any detector error. ---
    candidates: list[Span] = []
    try:
        for detector in active:
            candidates.extend(detector.detect(text))
    except Exception as err:  # noqa: BLE001 — fail closed (Req 1.7)
        logger.warning("Detector raised during redaction; failing closed: %s", err)
        latency_ms = (time.perf_counter() - start) * 1000.0
        return RedactionResult(
            redacted_text="",              # never leak unredacted text (Req 1.7)
            redactions=(),
            category_counts=MappingProxyType({}),
            chars_redacted=0,
            latency_ms=latency_ms,
            ok=False,
        )

    # --- 3. Resolve overlaps to a non-overlapping set. ---
    surviving = resolve_overlaps(candidates)

    # --- 8. No spans → original text unchanged. ---
    if not surviving:
        latency_ms = (time.perf_counter() - start) * 1000.0
        return RedactionResult(
            redacted_text=text,
            redactions=(),
            category_counts=MappingProxyType({}),
            chars_redacted=0,
            latency_ms=latency_ms,
            ok=True,
        )

    # --- 4. Splice placeholders right-to-left (start descending). ---
    # ``surviving`` is ascending; iterate its reverse so we cut the rightmost
    # span first and every earlier span's offsets stay valid.
    result_text = text
    category_counts: dict[str, int] = {}
    chars_redacted = 0
    for span in reversed(surviving):
        placeholder = placeholder_for(span.category)
        result_text = result_text[: span.start] + placeholder + result_text[span.end :]

        length = span.end - span.start
        chars_redacted += length
        category_counts[span.category] = category_counts.get(span.category, 0) + 1

        # --- 5. Telemetry sync: fire BOTH side-effects once per replacement. ---
        if audit is not None:
            audit.append(
                span.category,
                session_id,
                length=length,
                placeholder=placeholder,
            )
        if state is not None:
            state.record(session_id, span.category)

    latency_ms = (time.perf_counter() - start) * 1000.0

    # --- 7. Freeze into the immutable result (redactions ascending). ---
    return RedactionResult(
        redacted_text=result_text,
        redactions=tuple(surviving),
        category_counts=MappingProxyType(dict(category_counts)),
        chars_redacted=chars_redacted,
        latency_ms=latency_ms,
        ok=True,
    )
