"""Deterministic regression tests for placeholder survival through compression.

Covers task 10.3 of the pre-inference-redaction spec: run each placeholder in
the mapping through ``compress_prompt_detailed`` and assert byte-for-byte
survival, plus a repeated-value case asserting all identical placeholders
survive. Locks the Option A placeholder format against future compressor edits.
Validates Req 11.1, 11.3, 11.4.
"""

import pytest

from compressor import compress_prompt_detailed
from redactor import PLACEHOLDERS


@pytest.mark.parametrize("category,placeholder", list(PLACEHOLDERS.items()))
def test_each_placeholder_survives_byte_for_byte(category: str, placeholder: str) -> None:
    """Each category's placeholder passes through compression unaltered, even
    when embedded in prose that the compressor would otherwise trim."""
    text = (
        f"Please build a modern website and store the {placeholder} value "
        f"securely for the user account during deployment to the backend."
    )
    compressed = compress_prompt_detailed(text)["compressed"]
    assert placeholder in compressed
    assert compressed.count(placeholder) == 1


def test_placeholder_alone_survives() -> None:
    """A placeholder as the sole content survives compression."""
    for placeholder in PLACEHOLDERS.values():
        compressed = compress_prompt_detailed(placeholder)["compressed"]
        assert placeholder in compressed


def test_repeated_identical_placeholder_all_survive() -> None:
    """Repeated identical placeholders (from a repeated sensitive value) all
    survive byte-for-byte — the compressor performs no content-level dedup."""
    email = PLACEHOLDERS["email"]
    text = (
        f"Contact the user at {email} and also notify {email} plus escalate to "
        f"{email} for the modern website deployment."
    )
    compressed = compress_prompt_detailed(text)["compressed"]
    assert compressed.count(email) == 3


def test_mixed_placeholders_in_json_like_text_survive() -> None:
    """Placeholders inside JSON-like structural text survive intact, exercising
    the reconstruction step's spacing rules around { } [ ] : , without touching
    the placeholder characters themselves."""
    ssn = PLACEHOLDERS["ssn"]
    person = PLACEHOLDERS["person"]
    text = f'{{"owner": "{person} with ssn {ssn}", "features": "auth, payments"}}'
    compressed = compress_prompt_detailed(text)["compressed"]
    assert ssn in compressed
    assert person in compressed
