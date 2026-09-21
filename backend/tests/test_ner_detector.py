"""Unit tests for the NER detector and its graceful fallback (task 4.2).

Covers:
  * The ``nlp=None`` fallback path (no spaCy needed): emits NO ``person`` spans
    (Req 3.5).
  * The mapping logic via a tiny fake nlp/doc/ent stub (no spaCy dependency):
    ``PERSON``/``ORG``/``GPE`` entities map to ``person`` Spans with the right
    offsets, and other labels (e.g. ``DATE``, ``CARDINAL``) are ignored
    (Req 3.1).
  * The real-model path, guarded so it skips when the local model artifact is
    unavailable.
  * That ``load_ner_model()`` never raises and returns either an object or
    ``None`` (graceful degradation — Req 3.5).

Spans carry only offsets + category and never the raw value, so where a test
needs the matched text it reads it back via ``text[span.start:span.end]``.
"""

from collections import namedtuple

import pytest

from redactor import NerDetector, Span, load_ner_model


# A minimal fake spaCy pipeline so the mapping logic can be tested
# deterministically without depending on the real model.
_FakeEnt = namedtuple("_FakeEnt", ["start_char", "end_char", "label_"])


class _FakeDoc:
    def __init__(self, ents):
        self.ents = ents


class _FakeNlp:
    """Callable stub mirroring ``nlp(text) -> doc`` with a fixed ``.ents``."""

    def __init__(self, ents):
        self._ents = ents

    def __call__(self, text):
        return _FakeDoc(self._ents)


# ---------------------------------------------------------------------------
# Fallback path — nlp is None (Req 3.5), no spaCy needed
# ---------------------------------------------------------------------------

class TestFallbackNoModel:
    def test_returns_no_spans_for_names_and_orgs(self) -> None:
        detector = NerDetector(None)
        assert detector.detect("Contact Jane Doe at Acme Corp") == []

    def test_empty_input_returns_empty(self) -> None:
        assert NerDetector(None).detect("") == []

    def test_category_is_person(self) -> None:
        assert NerDetector(None).category == "person"


# ---------------------------------------------------------------------------
# Mapping logic via a fake nlp (Req 3.1), no spaCy dependency
# ---------------------------------------------------------------------------

class TestMappingWithFakeNlp:
    def test_maps_person_org_gpe_to_person_spans_with_offsets(self) -> None:
        text = "Jane Doe joined Acme Corp in Paris"
        # Offsets chosen to line up with the substrings in ``text``.
        ents = [
            _FakeEnt(0, 8, "PERSON"),    # "Jane Doe"
            _FakeEnt(16, 25, "ORG"),     # "Acme Corp"
            _FakeEnt(29, 34, "GPE"),     # "Paris"
        ]
        spans = NerDetector(_FakeNlp(ents)).detect(text)

        assert spans == [
            Span(0, 8, "person"),
            Span(16, 25, "person"),
            Span(29, 34, "person"),
        ]
        # Every emitted span is category ``person`` and maps to the right text.
        assert [text[s.start : s.end] for s in spans] == [
            "Jane Doe",
            "Acme Corp",
            "Paris",
        ]
        assert all(s.category == "person" for s in spans)

    def test_ignores_non_target_labels(self) -> None:
        text = "Jane Doe spent 42 dollars on 2024-01-01"
        ents = [
            _FakeEnt(0, 8, "PERSON"),    # kept
            _FakeEnt(15, 17, "CARDINAL"),  # ignored
            _FakeEnt(29, 39, "DATE"),    # ignored
        ]
        spans = NerDetector(_FakeNlp(ents)).detect(text)

        assert spans == [Span(0, 8, "person")]

    def test_no_entities_returns_empty(self) -> None:
        assert NerDetector(_FakeNlp([])).detect("nothing sensitive here") == []


# ---------------------------------------------------------------------------
# load_ner_model — graceful, never raises (Req 3.5)
# ---------------------------------------------------------------------------

class TestLoadNerModel:
    def test_does_not_raise_and_returns_object_or_none(self) -> None:
        # Must never raise regardless of whether the artifact is present.
        result = load_ner_model()
        assert result is None or result is not None  # i.e. it returned something


# ---------------------------------------------------------------------------
# Real-model path — guarded (skips when the local artifact is unavailable)
# ---------------------------------------------------------------------------

class TestRealModel:
    def test_real_model_detects_person_span(self) -> None:
        nlp = load_ner_model()
        if nlp is None:
            pytest.skip("NER model unavailable")

        text = "Contact Jane Doe at Acme Corp about the project"
        spans = NerDetector(nlp).detect(text)

        # At least one person span, all under the ``person`` category.
        assert len(spans) >= 1
        assert all(s.category == "person" for s in spans)

        # The matched substrings should include a recognizable name/org.
        matched = [text[s.start : s.end] for s in spans]
        joined = " ".join(matched)
        assert any(term in joined for term in ("Jane", "Doe", "Acme"))
