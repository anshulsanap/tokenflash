# Feature: hardened-semantic-cache, Property 1: no raw or redacted-prompt content ever reaches any cache sink
"""MANDATORY Property 1 (Privacy) for the hardened semantic cache.

The privacy invariant: whatever text is handed to the cache stage as the
"redacted prompt" must NEVER appear as readable text in ANY cache sink — it is
represented only by its embedding vector plus an opaque id. The sinks are the
REAL ones (never mocks):

  * the stored ChromaDB ``document`` and every stored ChromaDB ``metadata``
    value (a real persistent collection at a tmp dir, opened via
    ``semantic_cache.open_vector_store``),
  * every line of the real ``cache_log.CacheLog`` JSONL file at a tmp path,
  * the real ``cache_report`` and ``cache_benchmark`` annotation payload dicts
    (constructed inline exactly per the design annotation contract, since the
    main.py frame-builders are task 12 and have not been written yet).

Strategy: generate a distinctive redacted-style prompt ``P`` (letters/digits,
length >= 8, wrapped in ``ZZ...ZZ`` markers to hunt for leaks), embed it, and
``store()`` a Cache_Entry whose ``document`` is a RESULT text ``R`` (distinct
from ``P``) with scalar-only metadata. Append a ``cache_log`` decision entry and
build the ``cache_report`` + ``cache_benchmark`` annotation dicts. Then assert
``P`` — and every length-4 window of ``P`` — is absent from all sinks, and that
the stored document equals ``R`` (the prompt is retrievable only as the
embedding + id).

Validates Requirements 1.2, 2.4, 4.3, 5.11, 6.4, 7.5, 9.2, 9.8.

The real ``all-MiniLM-L6-v2`` model is loaded ONCE at module scope; when it is
unavailable the whole module SKIPS (never fails) so the invariant artifact is
not produced spuriously.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import semantic_cache
from cache_log import CacheLog

# Load the real embedding model ONCE for the module; skip everything if absent.
_MODEL = semantic_cache.load_embedding_model()
pytestmark = pytest.mark.skipif(
    _MODEL is None, reason="embedding model unavailable"
)


def _windows(value: str, size: int = 4):
    """Yield every substring of length ``size`` (a sliding window)."""
    for i in range(len(value) - size + 1):
        yield value[i : i + size]


# A distinctive redacted-style prompt, length >= 8, wrapped in ZZ...ZZ markers
# so any leak of even a fragment is easy to detect.
#
# The core is drawn from LETTERS only (not digits). Rationale: the cache sinks
# legitimately contain a small fixed set of non-sensitive scalar NUMBERS —
# token counts, char lengths, latency, and the zeros in the annotation payloads
# (e.g. "computeTimeSavedMs": 0). Those numbers are independent of the prompt,
# so a digit window like "0000" from a prompt core of "00000000" coincidentally
# matching a numeric metadata value is a test artifact, not a content leak. A
# letters-only core (still made distinctive by the ZZ markers) hunts for genuine
# leaks of prompt TEXT into any sink while avoiding that spurious digit collision
# — mirroring the audit-log Property 5 handling of independent numeric fields.
_prompt_core = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    min_size=8,
    max_size=40,
)
# The RESULT text stored as the document — arbitrary text (may be anything a
# model would produce). Kept distinct from P by construction in the test body.
_result_text = st.text(min_size=1, max_size=120)
_session_ids = st.uuids().map(str)


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(core=_prompt_core, result=_result_text, session_id=_session_ids)
def test_property_1_no_prompt_content_reaches_any_cache_sink(core, result, session_id):
    """No readable fragment of the redacted prompt P lands in any cache sink."""
    # Build a distinctive redacted prompt P with hunt markers.
    P = f"ZZ{core}ZZ"
    # Ensure the result text R is distinct from P (so an equality with the
    # stored document proves the document is R, not P).
    R = result if result != P else result + "_R"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # -- Real sinks --------------------------------------------------
        collection = semantic_cache.open_vector_store(path=str(tmp_path / "chroma"))
        assert collection is not None, "vector store failed to open"

        log = CacheLog(path=str(tmp_path / "cache_decisions.jsonl"))

        # Embed the REDACTED prompt P (the only representation of P allowed in
        # the store is this vector).
        embedding = semantic_cache.embed(_MODEL, P)

        # Scalar-only metadata per the design Cached_Result schema. No prompt
        # text of any kind.
        metadata = {
            "task_mode": "perform",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "real_input_tokens": 42,
            "real_output_tokens": 17,
            "real_total_tokens": 59,
            "inference_time_ms": 123,
            "result_char_len": len(R),
        }

        # Store the entry: document = RESULT text R (never P).
        assert semantic_cache.store(collection, embedding, R, metadata) is True

        # Append a real cache_log decision entry (scores/metadata only).
        assert log.append(
            session_id,
            "miss",
            top_score=0.5,
            runner_up_score=0.0,
            margin=0.5,
            had_runner_up=False,
        ) is True

        # Build the cache_report + cache_benchmark annotation dicts exactly per
        # the design annotation contract (scalar telemetry only — no prompt).
        cache_report_annotation = {
            "event": "cache_report",
            "sessionId": session_id,
            "stageEnabled": True,
            "hit": False,
            "hits": 0,
            "misses": 1,
            "decisions": 1,
            "hitRate": 0.0,
            "tokensSavedFromCache": 0,
            "computeTimeSavedMs": 0,
        }
        cache_benchmark_annotation = {
            "event": "cache_benchmark",
            "sessionId": session_id,
            "decision": "miss",
            "lookupLatencyMs": 1.5,
            "tokensSaved": 0,
            "inferenceTimeSavedMs": 0,
        }

        # -- Collect every sink's stored/readable text -------------------
        stored = collection.get(include=["documents", "metadatas"])
        stored_documents = stored["documents"]
        stored_metadatas = stored["metadatas"]

        # The document must be exactly R; the prompt is present only as the
        # embedding vector + opaque id.
        assert stored_documents == [R]
        assert P not in stored_documents[0]

        # The stored document is the RESULT text R — legitimate result content
        # that is independent of the prompt P and may be arbitrary text. It is
        # checked directly (equals R, and does not contain P) above/below rather
        # than window-scanned, since a coincidental 4-char overlap between
        # independently generated result text and P is a test artifact, not a
        # prompt leak. Every OTHER sink (metadata, ids, log lines, annotations)
        # must carry no prompt content at all and IS window-scanned.
        sink_strings: list[str] = []
        # Chroma metadata values (stringified).
        for meta in stored_metadatas:
            sink_strings.extend(str(v) for v in meta.values())
            # ids come back separately; include them defensively — they must be
            # opaque uuids, never P.
        sink_strings.extend(str(i) for i in stored["ids"])
        # cache_log JSONL lines.
        with open(log.path, "r", encoding="utf-8") as handle:
            log_lines = [ln for ln in handle.read().splitlines() if ln]
        sink_strings.extend(log_lines)
        # Annotation payloads (as JSON, the form they stream in).
        sink_strings.append(json.dumps(cache_report_annotation, ensure_ascii=False))
        sink_strings.append(json.dumps(cache_benchmark_annotation, ensure_ascii=False))

        haystack = "\x00".join(sink_strings)

        # Full-string absence.
        assert P not in haystack, f"the redacted prompt P leaked verbatim into a cache sink"

        # Core no-leak proof: NO length-4 window of P appears in any sink.
        for window in _windows(P, 4):
            assert window not in haystack, (
                f"4-char window {window!r} of the redacted prompt leaked into a "
                f"cache sink"
            )

        # Each cache_log line is valid JSON and carries no prompt field.
        for line in log_lines:
            entry = json.loads(line)
            assert set(entry.keys()) == {
                "timestamp",
                "session_id",
                "decision",
                "top_score",
                "runner_up_score",
                "margin",
                "had_runner_up",
            }
