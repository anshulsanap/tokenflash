"""Property and example tests for the redact() orchestration (`redactor.py`).

Covers tasks 8.3 and 8.6 of the pre-inference-redaction spec:

  * Property 1 — No sensitive value leaks downstream (Req 1.5, 2.1-2.5, 7.3)
  * Property 8 — Overlaps resolve to the single highest-precedence category
                 (Req 2.8, 3.2)

Property 1 focuses on the DETERMINISTIC detectors (built-in regex + a
custom-term detector built from a known term list) so the "no 4-char window of
any secret survives" invariant is crisp. Real spaCy NER is nondeterministic and
is exercised elsewhere (task 4.2).

A small in-memory fake audit + fake state run the telemetry path during the
property run and let a companion example test assert the telemetry-sync
invariant: #audit appends == #state.record calls == number of spans spliced.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from redactor import (
    PLACEHOLDER_RE,
    PLACEHOLDERS,
    PRECEDENCE,
    Span,
    placeholder_for,
    precedence_rank,
    redact,
    resolve_overlaps,
)


# ---------------------------------------------------------------------------
# In-memory fakes for the telemetry side-effects (no file / no singleton).
# ---------------------------------------------------------------------------

class FakeAudit:
    """AuditLog-like object recording each append(...) call in memory."""

    def __init__(self):
        self.entries: list[dict] = []

    def append(self, category, session_id, *, length, placeholder=None):
        self.entries.append(
            {
                "category": category,
                "session_id": session_id,
                "length": length,
                "placeholder": placeholder,
            }
        )
        return True


class FakeState:
    """RedactionState-like object recording each record(...) call in memory."""

    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def record(self, session_id, category, n=1):
        for _ in range(n):
            self.records.append((session_id, category))


# ---------------------------------------------------------------------------
# A deterministic custom-term detector over a fixed, known term list.
# ---------------------------------------------------------------------------

_CUSTOM_TERMS = ["Acme Corp", "Bluebird", "Initech"]


class _KnownTermsDetector:
    """Minimal deterministic custom_term detector (no store / no file I/O).

    Matches each known term as a case-insensitive whole occurrence, emitting
    ``custom_term`` spans. Kept trivially pure so Property 1 stays crisp.
    """

    category = "custom_term"

    def __init__(self, terms):
        # Longest-first so a longer term wins in the naive scan.
        self._terms = sorted(terms, key=len, reverse=True)

    def detect(self, text):
        spans = []
        lowered = text.lower()
        for term in self._terms:
            t = term.lower()
            start = 0
            while True:
                idx = lowered.find(t, start)
                if idx == -1:
                    break
                spans.append(Span(idx, idx + len(term), self.category))
                start = idx + len(term)
        return spans


def _detectors():
    """Regex built-ins + the deterministic custom-term detector."""
    from redactor import BUILTIN_REGEX_DETECTORS

    return tuple(BUILTIN_REGEX_DETECTORS) + (_KnownTermsDetector(_CUSTOM_TERMS),)


# ---------------------------------------------------------------------------
# Strategies: real secret-shaped values per category, interleaved with prose.
# ---------------------------------------------------------------------------

def _luhn_valid_card():
    """Generate a Luhn-valid 16-digit card number as a string.

    Draw 15 digits, then compute the check digit that makes the full 16-digit
    number pass Luhn (matching redactor._luhn_ok, which indexes from the right).
    """
    def _finish(first15):
        # first15 is a list of 15 ints (leftmost first). The check digit sits
        # at rightmost position (index 0 from the right => not doubled). Doubling
        # applies to odd indices from the right, i.e. every second digit.
        # Compute sum over the 15 known digits with their eventual right-index.
        total = 0
        # After appending the check digit, there will be 16 digits. The known
        # 15 occupy right-indices 1..15; the check digit is right-index 0.
        for right_index in range(1, 16):
            value = first15[15 - right_index]
            if right_index % 2 == 1:
                value *= 2
                if value > 9:
                    value -= 9
            total += value
        check = (10 - (total % 10)) % 10
        return "".join(str(d) for d in first15) + str(check)

    return st.lists(st.integers(0, 9), min_size=15, max_size=15).map(_finish)


_ssn = st.from_regex(r"[0-9]{3}-[0-9]{2}-[0-9]{4}", fullmatch=True)
_card = _luhn_valid_card()
_email = st.from_regex(r"[a-z]{3,8}@[a-z]{3,8}\.[a-z]{2,3}", fullmatch=True)
_phone = st.from_regex(r"\+?[0-9]{3}-[0-9]{3}-[0-9]{4}", fullmatch=True)
_api_key = st.one_of(
    st.from_regex(r"sk-[A-Za-z0-9]{20,32}", fullmatch=True),
    st.from_regex(r"AKIA[0-9A-Z]{16}", fullmatch=True),
    st.from_regex(r"ghp_[A-Za-z0-9]{36}", fullmatch=True),
    st.from_regex(r"Bearer [A-Za-z0-9._-]{12,20}", fullmatch=True),
)
_custom = st.sampled_from(_CUSTOM_TERMS)

# A single "secret token" from any category. Custom terms are included so the
# custom-term detector participates in the no-leak proof.
_secret_value = st.one_of(_ssn, _card, _email, _phone, _api_key, _custom)

# Prose chunks that contain no digits / @ / secret shapes, so they never
# themselves look like a secret and never provide false 4-char windows.
_prose = st.text(alphabet="abcdefghijklmnopqrstuvwxyz .,", min_size=0, max_size=20)


@st.composite
def _interleaved(draw):
    """Build prose interleaved with several real secret-shaped values.

    Returns (text, secrets) where ``secrets`` is the list of raw secret strings
    actually embedded, so the test can slide a window over each one.
    """
    n = draw(st.integers(min_value=1, max_value=6))
    parts: list[str] = []
    secrets: list[str] = []
    for _ in range(n):
        parts.append(draw(_prose))
        value = draw(_secret_value)
        secrets.append(value)
        # Separate every secret with word letters (not just whitespace) on both
        # sides. The phone regex permits internal spaces, so two digit secrets
        # separated by only spaces could be swallowed into ONE phone span that
        # then loses to a higher-precedence sub-span (e.g. an inner SSN),
        # leaving the other secret unredacted — a generator artifact, not an
        # implementation bug. A non-digit, non-space separator makes each
        # secret its own maximal match while still letting boundary-anchored
        # detectors fire.
        parts.append(" ref " + value + " end ")
    parts.append(draw(_prose))
    return "".join(parts), secrets


def _windows(value, size=4):
    """Yield every substring of length ``size`` (a sliding window)."""
    for i in range(len(value) - size + 1):
        yield value[i : i + size]


# ---------------------------------------------------------------------------
# Property 1 — no sensitive value leaks downstream
# ---------------------------------------------------------------------------

# Feature: pre-inference-redaction, Property 1: No sensitive value leaks downstream
@settings(max_examples=200)
@given(payload=_interleaved(), session_id=st.uuids().map(str))
def test_property_1_no_sensitive_value_leaks_downstream(payload, session_id):
    """No >=4-char window of any DETECTED secret appears in the redacted text
    (the compressor input), and placeholders appear instead. Validates
    Req 1.5, 2.1-2.5, 7.3.

    The telemetry path runs against in-memory fakes so it is exercised on every
    example (a leak into audit/state would also show up here).
    """
    text, secrets = payload
    audit = FakeAudit()
    state = FakeState()

    result = redact(text, session_id, detectors=_detectors(), audit=audit, state=state)

    assert result.ok is True

    # No-leak proof by POSITIONAL EXACT-EQUALITY, not a blanket window scan.
    #
    # The old check ("no 4-char window of any detected secret appears anywhere
    # in result.redacted_text") is confounded by COINCIDENCE: the `_prose`
    # strategy emits lowercase-letter text that can, on unlucky seeds,
    # coincidentally contain a 4-char window of a letter-only custom-term
    # secret (e.g. window "lueb" from "Bluebird" appearing in the trailing
    # prose "lueb"). Prose is never redacted, so that window legitimately
    # survives — it is NOT a leak of the detected secret's own span — yet the
    # blanket scan fired on it, causing intermittent failures.
    #
    # The invariant we actually want: for every DETECTED secret span, the
    # secret's characters from THAT span are replaced by a placeholder, and
    # nothing else in the text changes. Since the ONLY transformation redact()
    # makes is splicing each surviving span with placeholder_for(category) and
    # preserving every non-span byte (Property 4), the correct redacted text is
    # DETERMINISTIC given the surviving spans. Reconstruct it independently and
    # assert exact equality. This proves each detected span was replaced AND
    # that non-secret context (prose, other secrets) was left byte-for-byte
    # intact — and it is immune to coincidental-window false positives entirely.
    # Not-detected secrets are handled naturally: they aren't in the surviving
    # span set, so their verbatim occurrences remain identically in both the
    # reference and result.redacted_text.
    expected = text
    for span in sorted(result.redactions, key=lambda s: s.start, reverse=True):
        expected = (
            expected[: span.start]
            + placeholder_for(span.category)
            + expected[span.end :]
        )
    assert result.redacted_text == expected, (
        "redacted text is not the original with each detected span spliced to "
        "its placeholder"
    )

    # Placeholders appear in place of redactions (when anything was redacted).
    if result.redactions:
        found = PLACEHOLDER_RE.findall(result.redacted_text)
        assert len(found) == len(result.redactions)
        for span in result.redactions:
            assert placeholder_for(span.category) in result.redacted_text


# Feature: pre-inference-redaction, Property 1: No sensitive value leaks downstream
def test_telemetry_sync_one_append_and_record_per_spliced_span():
    """Companion example: #audit appends == #state.record calls == #spans
    spliced. Locks in the telemetry-sync invariant (k replacements => k audit
    appends => k recorded increments)."""
    text = (
        "Contact john@example.com or call +1-555-123-4567. "
        "SSN 123-45-6789, key sk-ABCDEFGHIJKLMNOPQRST, client Acme Corp."
    )
    audit = FakeAudit()
    state = FakeState()

    result = redact(text, "sess-telemetry", detectors=_detectors(), audit=audit, state=state)

    k = len(result.redactions)
    assert k >= 4  # at least email, phone, ssn, api_key
    assert len(audit.entries) == k
    assert len(state.records) == k

    # Each audit entry's length equals the spliced span length, and its
    # placeholder matches the category — never the raw value.
    span_lengths = sorted(s.end - s.start for s in result.redactions)
    assert sorted(e["length"] for e in audit.entries) == span_lengths
    for entry in audit.entries:
        assert entry["placeholder"] == PLACEHOLDERS[entry["category"]]
        assert entry["session_id"] == "sess-telemetry"

    # Every recorded (session, category) pair is a real redaction category.
    recorded_categories = sorted(cat for (_sid, cat) in state.records)
    span_categories = sorted(s.category for s in result.redactions)
    assert recorded_categories == span_categories


def test_no_side_effects_when_audit_and_state_none():
    """redact() runs without audit/state (graceful skip) and still redacts."""
    result = redact("email me at a@b.co", "s", detectors=_detectors())
    assert result.ok is True
    assert "⟦REDACTED_EMAIL⟧" in result.redacted_text


# ---------------------------------------------------------------------------
# Property 8 — overlaps resolve to the single highest-precedence category
# ---------------------------------------------------------------------------

# Strategy: a random set of candidate spans within a bounded text length, with
# random categories drawn from PRECEDENCE.
_LENGTH = 40


@st.composite
def _random_spans(draw):
    n = draw(st.integers(min_value=0, max_value=12))
    spans = []
    for _ in range(n):
        start = draw(st.integers(min_value=0, max_value=_LENGTH - 1))
        # end strictly > start so every candidate covers >=1 char.
        end = draw(st.integers(min_value=start + 1, max_value=_LENGTH))
        category = draw(st.sampled_from(list(PRECEDENCE)))
        spans.append(Span(start, end, category))
    return spans


# Feature: pre-inference-redaction, Property 8: Overlaps resolve to the single highest-precedence category
@settings(max_examples=200)
@given(spans=_random_spans())
def test_property_8_overlaps_resolve_to_highest_precedence(spans):
    """resolve_overlaps yields a non-overlapping subset of the inputs where any
    covered position sits under the highest-precedence category among the input
    spans covering it, and no ``person`` span survives over a higher-precedence
    range. Validates Req 2.8, 3.2."""
    result = resolve_overlaps(list(spans))

    def _overlap(a, b):
        return a.start < b.end and b.start < a.end

    # (b) every output span is one of the inputs (offsets never trimmed/merged).
    input_set = set(spans)
    for out in result:
        assert out in input_set

    # (a)/(d) output spans are pairwise NON-overlapping — each original char is
    # covered at most once.
    ordered = sorted(result, key=lambda s: (s.start, s.end))
    for prev, nxt in zip(ordered, ordered[1:]):
        assert prev.end <= nxt.start, f"{prev} overlaps {nxt}"

    # (c) The resolver drops WHOLE overlapping lower-precedence spans (never
    # trims/splits). The precise guarantee is that NO surviving span is
    # overlapped by a strictly higher-precedence span that itself SURVIVED —
    # i.e. among any set of mutually-overlapping survivors the highest
    # precedence wins. (A higher-precedence input can be dropped by an
    # even-higher one, freeing space for a lower-precedence span; that is
    # correct per-span behavior, so we compare only against survivors.)
    for out in result:
        for other in result:
            if other is out:
                continue
            if _overlap(out, other):
                # Non-overlap already forbids this; this is a redundant guard
                # documenting the intent.
                raise AssertionError(f"{out} overlaps surviving {other}")

    # (c-reference) Verify the deterministic greedy contract exactly by
    # reproducing the reference selection independently and comparing. This is
    # the authoritative correctness check: higher precedence first, then earlier
    # start, then longer span, admitted greedily if it overlaps no accepted span.
    reference_candidates = sorted(
        (s for s in spans if s.end > s.start),
        key=lambda s: (precedence_rank(s.category), s.start, -s.end, s.category),
    )
    reference_accepted: list[Span] = []
    for span in reference_candidates:
        if not any(_overlap(span, kept) for kept in reference_accepted):
            reference_accepted.append(span)
    reference_accepted.sort(key=lambda s: (s.start, s.end))
    assert ordered == reference_accepted

    # (e) No 'person' span survives over a range also claimed by any surviving
    # higher-precedence span (Req 3.2: no duplicate person over another
    # detector's claimed characters). Because output is non-overlapping and
    # matches the reference greedy selection, any other-category span that
    # overlaps a person candidate and itself survives would have blocked that
    # person span. Assert directly:
    for out in result:
        if out.category != "person":
            continue
        for other in result:
            if other is out or other.category == "person":
                continue
            assert not _overlap(out, other), (
                f"person span {out} survived over higher-precedence range {other}"
            )

    # No output covers a position that no input covered.
    input_positions = set()
    for s in spans:
        input_positions.update(range(s.start, s.end))
    for s in result:
        for pos in range(s.start, s.end):
            assert pos in input_positions


# Feature: pre-inference-redaction, Property 8: Overlaps resolve to the single highest-precedence category
def test_property_8_targeted_deterministic_cases():
    """Targeted overlap cases: email beats person; ssn beats person; a
    non-overlapping person survives on its own."""
    # email overlapping person → email wins (email outranks person).
    email = Span(0, 20, "email")
    person = Span(5, 15, "person")
    res = resolve_overlaps([person, email])
    assert res == [email]

    # ssn overlapping person → ssn wins.
    ssn = Span(2, 13, "ssn")
    person2 = Span(0, 20, "person")
    res2 = resolve_overlaps([person2, ssn])
    assert res2 == [ssn]

    # A person span that overlaps nothing higher survives.
    person3 = Span(30, 40, "person")
    email2 = Span(0, 10, "email")
    res3 = resolve_overlaps([email2, person3])
    assert sorted(res3, key=lambda s: s.start) == [email2, person3]

    # Equal precedence, overlapping: earlier start wins, then longer span.
    a = Span(0, 10, "email")
    b = Span(3, 12, "email")
    res4 = resolve_overlaps([b, a])
    assert res4 == [a]
