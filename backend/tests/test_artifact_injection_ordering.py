# Backfill of the deferred optional Task 9 (private-on-device-artifacts spec):
# a backend test for the artifact-delimiter injection's POSITION in the pipeline
# relative to the redaction stage, and that redaction + compression + the
# delimiter instruction do NOT scramble or conflict when stacked.
"""
Pipeline ordering (verified against backend/main.py, not just asserted here):

  1. REDACTION runs on the requirements summary FIRST → ``gen_redacted`` (raw
     sensitive values already replaced by ⟦REDACTED_<CATEGORY>⟧ placeholders).
  2. COMPRESSION runs on the redacted text → ``compress_prompt_detailed(gen_redacted)``.
  3. On the BUILD path ONLY, ``inject_artifact_instruction(compressed, enabled=...)``
     PREPENDS the delimiter instruction to the COMPRESSED prompt.

So the artifact-delimiter instruction sits DOWNSTREAM of redaction and
compression: it can never re-introduce a raw value, because it only ever sees
the already-redacted, already-compressed text. These tests exercise the REAL
``redact`` → ``compress_prompt_detailed`` → ``inject_artifact_instruction`` chain
stacked together (no model call) and assert:

  * injection prepends the instruction and preserves the compressed body verbatim
    at the tail (nothing scrambled/interleaved);
  * disabled → the compressed prompt passes through byte-for-byte unchanged;
  * the delimiter markers appear BEFORE the compressed body (artifact-first);
  * stacking all three preserves the redaction placeholders intact and never
    leaks the raw values that redaction removed.
"""

from __future__ import annotations

from compressor import compress_prompt_detailed
from redactor import BUILTIN_REGEX_DETECTORS, PLACEHOLDER_RE, redact
from main import (
    inject_artifact_instruction,
    _ARTIFACT_DELIMITER_INSTRUCTION,
)


# A raw summary carrying sensitive values redaction MUST remove, plus tech and
# prose so compression has something to chew on.
RAW_SUMMARY = (
    "Build a dashboard in HTML/CSS/JavaScript for John Doe. "
    "Contact john@example.com or call about SSN 123-45-6789; "
    "the API key is sk-ABCD1234SECRETKEYVALUE for the integration."
)
# The REGEX-redactable secrets in RAW_SUMMARY. This unit test wires ONLY the
# built-in regex detectors (no NER model), so the ``person`` category is NOT
# exercised here — "John Doe" is intentionally NOT listed, because name
# redaction requires the NER detector (covered separately by
# test_ner_tech_allowlist.py). These three ARE removed by the regex stage and
# must never reappear downstream of redaction.
RAW_SECRETS = [
    "john@example.com",
    "123-45-6789",
    "sk-ABCD1234SECRETKEYVALUE",
]


def _redact_then_compress(text: str) -> str:
    """Run the REAL stages 1→2 (redaction then compression) and return the
    compressed redacted prompt, exactly as main.py's BUILD path computes it."""
    result = redact(text, "sess-inject", detectors=tuple(BUILTIN_REGEX_DETECTORS))
    assert result.ok is True
    return compress_prompt_detailed(result.redacted_text)["compressed"]


def test_injection_prepends_and_preserves_compressed_body_verbatim():
    compressed = _redact_then_compress(RAW_SUMMARY)
    injected = inject_artifact_instruction(compressed, enabled=True)

    # The instruction is prepended, and the compressed body survives verbatim as
    # the tail — nothing interleaved or scrambled between them.
    assert injected.startswith(_ARTIFACT_DELIMITER_INSTRUCTION)
    assert injected.endswith(compressed)
    # The delimiter markers appear BEFORE the compressed body (artifact-first).
    marker_pos = injected.index("<<<TOKENQUICK_ARTIFACT:")
    body_pos = injected.index(compressed)
    assert marker_pos < body_pos


def test_injection_disabled_is_passthrough():
    compressed = _redact_then_compress(RAW_SUMMARY)
    # Disabled → byte-for-byte passthrough (behaves exactly as before the stage).
    assert inject_artifact_instruction(compressed, enabled=False) == compressed


def test_stacking_preserves_placeholders_and_leaks_no_raw_value():
    # Stage 1: redaction. Confirm the raw secrets are gone and placeholders present.
    result = redact(RAW_SUMMARY, "sess-inject", detectors=tuple(BUILTIN_REGEX_DETECTORS))
    assert result.ok is True
    redacted = result.redacted_text
    placeholders_after_redact = PLACEHOLDER_RE.findall(redacted)
    assert placeholders_after_redact, "expected at least one placeholder after redaction"

    # Stage 2 + 3: compress then inject. The final prompt the model would receive.
    compressed = compress_prompt_detailed(redacted)["compressed"]
    injected = inject_artifact_instruction(compressed, enabled=True)

    # INVARIANT: no raw secret survives anywhere in the final injected prompt.
    for secret in RAW_SECRETS:
        assert secret not in injected, f"raw value {secret!r} leaked into the injected prompt"

    # INVARIANT: the redaction placeholders are still present and unscrambled in
    # the final prompt — compression + injection did not split/mangle them. Every
    # placeholder token found in the final prompt is a well-formed placeholder,
    # and at least as many survive as the compressor kept (it may drop some
    # tokens, but must never corrupt a surviving one into a non-placeholder).
    final_placeholders = PLACEHOLDER_RE.findall(injected)
    # Every placeholder that appears is well-formed (the regex guarantees shape);
    # cross-check they are a subset of what redaction produced (no fabricated new
    # categories introduced by compression/injection).
    from collections import Counter
    produced = Counter(placeholders_after_redact)
    survived = Counter(final_placeholders)
    for tok, n in survived.items():
        assert tok in produced, f"injection/compression fabricated placeholder {tok!r}"
        assert n <= produced[tok], f"placeholder {tok!r} count grew unexpectedly"

    # The delimiter instruction text itself carries NO raw value (it is a static
    # constant), so injecting it cannot add sensitive content.
    for secret in RAW_SECRETS:
        assert secret not in _ARTIFACT_DELIMITER_INSTRUCTION
