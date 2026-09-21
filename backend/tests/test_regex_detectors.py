"""Unit tests for the built-in regex detectors (task 3.3).

Covers positive/negative cases per category, strict Luhn acceptance/rejection
for credit cards, independent matching of all four API-key forms, and offset
correctness. Validates Req 2.6 (local-only, no network — inherent to regex) and
Req 2.9 (no-match returns no spans).

Spans carry only offsets + category and never the raw value, so where a test
needs the matched text it reads it back via ``text[span.start:span.end]``.
"""

import pytest

from redactor import (
    ApiKeyDetector,
    CreditCardDetector,
    EmailDetector,
    PhoneDetector,
    SsnDetector,
    BUILTIN_REGEX_DETECTORS,
)


def _matched(text: str, spans) -> list[str]:
    """Read the matched substrings back out of the original text via offsets."""
    return [text[s.start : s.end] for s in spans]


# ---------------------------------------------------------------------------
# SSN (Req 2.1)
# ---------------------------------------------------------------------------

class TestSsnDetector:
    def test_matches_hyphen_delimited(self) -> None:
        text = "my ssn is 123-45-6789 ok"
        spans = SsnDetector().detect(text)
        assert _matched(text, spans) == ["123-45-6789"]
        assert all(s.category == "ssn" for s in spans)

    def test_matches_space_delimited(self) -> None:
        text = "ssn 123 45 6789 done"
        spans = SsnDetector().detect(text)
        assert _matched(text, spans) == ["123 45 6789"]

    def test_matches_multiple_in_one_pass(self) -> None:
        text = "first 111-22-3333 second 444-55-6666"
        spans = SsnDetector().detect(text)
        assert _matched(text, spans) == ["111-22-3333", "444-55-6666"]

    def test_does_not_match_bare_nine_digit_run(self) -> None:
        # Conservative: a bare 9-digit number must NOT be treated as an SSN.
        assert SsnDetector().detect("account 123456789 here") == []

    def test_no_match_returns_empty(self) -> None:
        assert SsnDetector().detect("nothing sensitive here") == []


# ---------------------------------------------------------------------------
# Credit card + Luhn (Req 2.2)
# ---------------------------------------------------------------------------

# Standard test PANs that are Luhn-valid and MUST be detected.
LUHN_VALID = [
    "4111111111111111",   # Visa
    "4012888888881881",   # Visa
    "5555555555554444",   # Mastercard
    "378282246310005",    # Amex (15 digits)
]

# Same-length digit runs that FAIL the Luhn checksum and MUST NOT be detected.
LUHN_INVALID = [
    "4111111111111112",   # Visa test number with a broken check digit
    "1234567890123456",   # arbitrary 16-digit run, checksum fails
]


class TestCreditCardDetector:
    @pytest.mark.parametrize("pan", LUHN_VALID)
    def test_luhn_valid_detected(self, pan: str) -> None:
        text = f"card {pan} on file"
        spans = CreditCardDetector().detect(text)
        assert _matched(text, spans) == [pan]
        assert spans[0].category == "credit_card"

    @pytest.mark.parametrize("pan", LUHN_INVALID)
    def test_luhn_invalid_rejected(self, pan: str) -> None:
        # Explicit Luhn rejection: no span emitted for a checksum failure.
        assert CreditCardDetector().detect(f"card {pan} on file") == []

    def test_spaced_card_detected_full_span(self) -> None:
        text = "pay with 4111 1111 1111 1111 today"
        spans = CreditCardDetector().detect(text)
        # The Span must cover the FULL substring including the spaces.
        assert _matched(text, spans) == ["4111 1111 1111 1111"]

    def test_hyphenated_card_detected_full_span(self) -> None:
        text = "pay with 4111-1111-1111-1111 today"
        spans = CreditCardDetector().detect(text)
        assert _matched(text, spans) == ["4111-1111-1111-1111"]

    def test_multiple_cards_one_pass(self) -> None:
        text = "a 4111111111111111 b 5555555555554444"
        spans = CreditCardDetector().detect(text)
        assert _matched(text, spans) == ["4111111111111111", "5555555555554444"]

    def test_no_match_returns_empty(self) -> None:
        assert CreditCardDetector().detect("no numbers worth noting") == []


# ---------------------------------------------------------------------------
# Email (Req 2.3)
# ---------------------------------------------------------------------------

class TestEmailDetector:
    def test_matches_simple_email(self) -> None:
        text = "reach me at jane@example.com please"
        spans = EmailDetector().detect(text)
        assert _matched(text, spans) == ["jane@example.com"]
        assert spans[0].category == "email"

    def test_matches_complex_local_and_subdomain(self) -> None:
        text = "send to a.b+tag@sub-domain.example.co.uk now"
        spans = EmailDetector().detect(text)
        assert _matched(text, spans) == ["a.b+tag@sub-domain.example.co.uk"]

    def test_matches_multiple(self) -> None:
        text = "x@a.com and y@b.org"
        spans = EmailDetector().detect(text)
        assert _matched(text, spans) == ["x@a.com", "y@b.org"]

    def test_does_not_match_bare_at_word(self) -> None:
        assert EmailDetector().detect("meet @ noon or at home") == []

    def test_no_match_returns_empty(self) -> None:
        assert EmailDetector().detect("plain prose, no addresses") == []


# ---------------------------------------------------------------------------
# Phone (Req 2.4)
# ---------------------------------------------------------------------------

class TestPhoneDetector:
    def test_matches_intl_paren_format(self) -> None:
        text = "call +1 (555) 123-4567 anytime"
        spans = PhoneDetector().detect(text)
        assert _matched(text, spans) == ["+1 (555) 123-4567"]
        assert spans[0].category == "phone"

    def test_matches_dotted_format(self) -> None:
        text = "ring 555.123.4567 please"
        spans = PhoneDetector().detect(text)
        assert _matched(text, spans) == ["555.123.4567"]

    def test_does_not_match_short_number(self) -> None:
        # Too few digits to be a phone number.
        assert PhoneDetector().detect("room 42 today") == []

    def test_no_match_returns_empty(self) -> None:
        assert PhoneDetector().detect("no digits here at all") == []


# ---------------------------------------------------------------------------
# API keys / tokens (Req 2.5) — each of the four forms matches independently
# ---------------------------------------------------------------------------

class TestApiKeyDetector:
    def test_sk_key_detected(self) -> None:
        text = "key sk-proj-ABCDEFGHIJKLMNOP1234 stored"
        spans = ApiKeyDetector().detect(text)
        assert _matched(text, spans) == ["sk-proj-ABCDEFGHIJKLMNOP1234"]
        assert spans[0].category == "api_key"

    def test_akia_key_detected(self) -> None:
        text = "aws AKIAIOSFODNN7EXAMPLE creds"
        spans = ApiKeyDetector().detect(text)
        assert _matched(text, spans) == ["AKIAIOSFODNN7EXAMPLE"]

    def test_ghp_token_detected(self) -> None:
        token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        text = f"github {token} token"
        spans = ApiKeyDetector().detect(text)
        assert _matched(text, spans) == [token]

    def test_bearer_token_detected_and_span_region(self) -> None:
        token = "Bearer abc123XYZ._-token"
        text = f"auth header {token} sent"
        spans = ApiKeyDetector().detect(text)
        assert len(spans) == 1
        # The span must cover the whole "Bearer <token>" region.
        assert _matched(text, spans) == [token]
        assert text[spans[0].start : spans[0].end] == token

    def test_all_four_forms_in_one_pass(self) -> None:
        text = (
            "sk-ABCDEFGHIJKLMNOP1234 "
            "AKIAIOSFODNN7EXAMPLE "
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "
            "Bearer abcdEFGH1234"
        )
        spans = ApiKeyDetector().detect(text)
        matched = _matched(text, spans)
        assert "sk-ABCDEFGHIJKLMNOP1234" in matched
        assert "AKIAIOSFODNN7EXAMPLE" in matched
        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" in matched
        assert "Bearer abcdEFGH1234" in matched
        assert len(spans) == 4

    def test_bare_bearer_word_not_matched(self) -> None:
        # "Bearer" with no following token must NOT match.
        assert ApiKeyDetector().detect("the Bearer of bad news") == []

    def test_short_sk_prefix_not_matched(self) -> None:
        # "sk-" with nothing (or too little) after it must NOT match.
        assert ApiKeyDetector().detect("sk- nothing here") == []

    def test_no_match_returns_empty(self) -> None:
        assert ApiKeyDetector().detect("ordinary sentence without secrets") == []


# ---------------------------------------------------------------------------
# Offset correctness (Spans index into the original text)
# ---------------------------------------------------------------------------

class TestOffsets:
    def test_email_offsets_index_original_text(self) -> None:
        text = "prefix jane@example.com suffix"
        span = EmailDetector().detect(text)[0]
        expected_start = text.index("jane@example.com")
        assert span.start == expected_start
        assert span.end == expected_start + len("jane@example.com")
        assert text[span.start : span.end] == "jane@example.com"

    def test_ssn_offsets_index_original_text(self) -> None:
        text = "id: 123-45-6789 end"
        span = SsnDetector().detect(text)[0]
        assert text[span.start : span.end] == "123-45-6789"
        assert span.start == text.index("123-45-6789")


# ---------------------------------------------------------------------------
# Shared behavior across all built-in regex detectors
# ---------------------------------------------------------------------------

class TestBuiltinCollection:
    def test_collection_exposes_five_categories(self) -> None:
        cats = [d.category for d in BUILTIN_REGEX_DETECTORS]
        assert cats == ["ssn", "credit_card", "api_key", "email", "phone"]

    @pytest.mark.parametrize("detector", BUILTIN_REGEX_DETECTORS)
    def test_empty_text_yields_no_spans(self, detector) -> None:
        assert detector.detect("") == []
