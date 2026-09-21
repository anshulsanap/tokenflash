"""Unit tests for the custom-terms store, matcher, and detector.

Covers task 5.4 of the pre-inference-redaction spec:
  * load & validation bounds (Req 4.1, 4.3, 4.9)
  * literal (injection-safe) regex matching with correct per-term boundaries
  * case-insensitivity, multiple matches in one pass, longest-term-wins
  * add/remove semantics with atomic persistence (Req 4.4–4.8, 5.2–5.6)
  * atomic-write failure retains in-memory list AND prior on-disk file
  * mtime-based live reload (Req 4.6)

Every test uses ``tmp_path`` for the config so the real
``backend/redaction_terms.json`` is never touched. Spans carry only offsets +
category, so matched text is read as ``text[span.start:span.end]``.
"""

from __future__ import annotations

import json
import os

import pytest

from custom_terms import (
    CATEGORY,
    MAX_TERM_LEN,
    CustomTermDetector,
    CustomTermsStore,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_config(path, terms):
    """Write a JSON array config at ``path``."""
    path.write_text(json.dumps(terms), encoding="utf-8")


def _matched_texts(store, text):
    """Return the literal substrings matched by the store's detector."""
    return [text[s.start:s.end] for s in store.detect(text)]


# ---------------------------------------------------------------------------
# load & validation (Req 4.1, 4.3, 4.9)
# ---------------------------------------------------------------------------

def test_valid_terms_load(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme Corp", "Bluebird"])
    store = CustomTermsStore(path=str(cfg))
    assert store.terms() == ["Acme Corp", "Bluebird"]


def test_empty_and_whitespace_terms_skipped_with_reason(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme", "", "   ", "Bluebird"])
    store = CustomTermsStore(path=str(cfg))
    assert store.terms() == ["Acme", "Bluebird"]
    reasons = " ".join(s["reason"] for s in store.skipped())
    assert "empty" in reasons


def test_too_long_term_skipped_with_reason(tmp_path):
    cfg = tmp_path / "terms.json"
    too_long = "x" * (MAX_TERM_LEN + 1)
    _write_config(cfg, ["Acme", too_long])
    store = CustomTermsStore(path=str(cfg))
    assert store.terms() == ["Acme"]
    assert any("256" in s["reason"] for s in store.skipped())


def test_case_insensitive_duplicate_skipped(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme", "ACME", "acme"])
    store = CustomTermsStore(path=str(cfg))
    assert store.terms() == ["Acme"]
    assert any("duplicate" in s["reason"] for s in store.skipped())


def test_missing_file_yields_empty_list_with_reason(tmp_path):
    cfg = tmp_path / "does_not_exist.json"
    store = CustomTermsStore(path=str(cfg))
    assert store.terms() == []
    assert store.detect("anything at all") == []
    assert any("not found" in s["reason"] for s in store.skipped())


def test_invalid_json_yields_empty_list_with_reason(tmp_path):
    cfg = tmp_path / "terms.json"
    cfg.write_text("{ this is not valid json ]", encoding="utf-8")
    store = CustomTermsStore(path=str(cfg))
    assert store.terms() == []
    assert any("JSON" in s["reason"] for s in store.skipped())


def test_non_array_root_yields_empty_list(tmp_path):
    cfg = tmp_path / "terms.json"
    cfg.write_text(json.dumps({"terms": ["Acme"]}), encoding="utf-8")
    store = CustomTermsStore(path=str(cfg))
    assert store.terms() == []
    assert any("array" in s["reason"] for s in store.skipped())


def test_empty_term_list_builds_no_catch_all(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, [])
    store = CustomTermsStore(path=str(cfg))
    # No regex is built for an empty list, so nothing is ever matched.
    assert store.detect("some text with words") == []


# ---------------------------------------------------------------------------
# regex safety: literal matching, injection resistance, boundaries
# ---------------------------------------------------------------------------

def test_special_char_terms_match_literally(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["C++", "Project [Omega]", "a.b.c", "$$$"])
    store = CustomTermsStore(path=str(cfg))

    # "C++" matches literally as a whole occurrence.
    text = "I love C++ today"
    spans = store.detect(text)
    assert len(spans) == 1
    assert text[spans[0].start:spans[0].end] == "C++"
    assert spans[0].category == CATEGORY

    # "Project [Omega]" — brackets are literal, not a character class.
    assert _matched_texts(store, "The Project [Omega] launch") == ["Project [Omega]"]

    # "$$$" literal.
    assert _matched_texts(store, "cost is $$$ here") == ["$$$"]


def test_dotted_term_does_not_act_as_regex_wildcard(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["a.b.c"])
    store = CustomTermsStore(path=str(cfg))
    # The '.' must be literal — it must NOT match "axbxc".
    assert store.detect("axbxc") == []
    assert _matched_texts(store, "see a.b.c now") == ["a.b.c"]


def test_boundary_for_non_word_ending_term(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["C++"])
    store = CustomTermsStore(path=str(cfg))
    # Ends in '+', a non-word char: matches even when followed by non-space.
    assert _matched_texts(store, "use C++ today") == ["C++"]
    assert _matched_texts(store, "we love C++.") == ["C++"]


def test_word_term_not_matched_inside_larger_word(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme"])
    store = CustomTermsStore(path=str(cfg))
    # "Acme" must NOT match inside "Acmelike" (word-char boundaries apply).
    assert store.detect("Acmelike products") == []
    assert _matched_texts(store, "the Acme brand") == ["Acme"]


def test_case_insensitive_matching(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["acme"])
    store = CustomTermsStore(path=str(cfg))
    assert _matched_texts(store, "ACME and Acme and acme") == ["ACME", "Acme", "acme"]


def test_multiple_matches_in_one_pass(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme", "Bluebird"])
    store = CustomTermsStore(path=str(cfg))
    text = "Acme met Bluebird then Acme left"
    assert _matched_texts(store, text) == ["Acme", "Bluebird", "Acme"]


def test_longest_term_wins_on_overlap(tmp_path):
    cfg = tmp_path / "terms.json"
    # "Acme Corp" should win over "Acme" where both could start.
    _write_config(cfg, ["Acme", "Acme Corp"])
    store = CustomTermsStore(path=str(cfg))
    assert _matched_texts(store, "the Acme Corp deal") == ["Acme Corp"]


# ---------------------------------------------------------------------------
# add / remove semantics + persistence (Req 4.4, 4.5, 4.7, 5.2–5.6)
# ---------------------------------------------------------------------------

def test_add_persists_and_new_store_sees_it(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme"])
    store = CustomTermsStore(path=str(cfg))
    store.add("Bluebird")
    # A fresh store reading the same path sees the persisted term.
    fresh = CustomTermsStore(path=str(cfg))
    assert "Bluebird" in fresh.terms()


def test_add_trims_whitespace(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, [])
    store = CustomTermsStore(path=str(cfg))
    store.add("  Padded Term  ")
    assert store.terms() == ["Padded Term"]


def test_duplicate_add_is_noop(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme"])
    store = CustomTermsStore(path=str(cfg))
    result = store.add("acme")  # case-insensitive duplicate
    assert result == ["Acme"]
    assert store.terms() == ["Acme"]


def test_add_empty_after_trim_raises(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, [])
    store = CustomTermsStore(path=str(cfg))
    with pytest.raises(ValueError):
        store.add("   ")


def test_add_too_long_raises(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, [])
    store = CustomTermsStore(path=str(cfg))
    with pytest.raises(ValueError):
        store.add("x" * (MAX_TERM_LEN + 1))


def test_remove_present_persists(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme", "Bluebird"])
    store = CustomTermsStore(path=str(cfg))
    updated, removed = store.remove("acme")  # case-insensitive
    assert removed is True
    assert updated == ["Bluebird"]
    fresh = CustomTermsStore(path=str(cfg))
    assert fresh.terms() == ["Bluebird"]


def test_remove_absent_is_noop(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme"])
    store = CustomTermsStore(path=str(cfg))
    updated, removed = store.remove("Nonexistent")
    assert removed is False
    assert updated == ["Acme"]


def test_detect_reflects_add_and_remove(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, [])
    store = CustomTermsStore(path=str(cfg))
    store.add("Bluebird")
    assert _matched_texts(store, "meet Bluebird now") == ["Bluebird"]
    store.remove("Bluebird")
    assert store.detect("meet Bluebird now") == []


# ---------------------------------------------------------------------------
# atomic write correctness + failure handling (Req 4.7, 4.8)
# ---------------------------------------------------------------------------

def test_add_writes_valid_complete_json(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme"])
    store = CustomTermsStore(path=str(cfg))
    store.add("Bluebird")
    # The config file is valid JSON containing the term (no partial file),
    # and no temp files were left behind.
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert "Bluebird" in on_disk
    leftovers = [p for p in os.listdir(tmp_path) if p != "terms.json"]
    assert leftovers == []


def test_persistence_failure_retains_memory_and_prior_file(tmp_path, monkeypatch):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme"])
    prior_bytes = cfg.read_bytes()
    store = CustomTermsStore(path=str(cfg))

    # Simulate an atomic-write failure at the final swap.
    def _boom(src, dst):
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(os, "replace", _boom)

    with pytest.raises(OSError):
        store.add("Bluebird")

    # In-memory list retained (rolled back to prior state).
    assert store.terms() == ["Acme"]
    # Prior on-disk file is byte-for-byte unchanged (not corrupt/half-written).
    assert cfg.read_bytes() == prior_bytes
    # No temp debris left behind.
    leftovers = [p for p in os.listdir(tmp_path) if p != "terms.json"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# live reload via mtime (Req 4.6)
# ---------------------------------------------------------------------------

def test_mtime_live_reload_picks_up_added_term(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme"])
    store = CustomTermsStore(path=str(cfg))
    assert _matched_texts(store, "hi Bluebird") == []

    # Rewrite the file directly and bump mtime deterministically.
    _write_config(cfg, ["Acme", "Bluebird"])
    future = os.stat(str(cfg)).st_mtime + 100
    os.utime(str(cfg), (future, future))

    store.maybe_reload()
    assert _matched_texts(store, "hi Bluebird") == ["Bluebird"]


def test_mtime_live_reload_drops_removed_term(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme", "Bluebird"])
    store = CustomTermsStore(path=str(cfg))
    assert _matched_texts(store, "hi Bluebird") == ["Bluebird"]

    _write_config(cfg, ["Acme"])
    future = os.stat(str(cfg)).st_mtime + 100
    os.utime(str(cfg), (future, future))

    # detect() runs maybe_reload() internally.
    assert store.detect("hi Bluebird") == []


def test_maybe_reload_handles_file_deleted(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme"])
    store = CustomTermsStore(path=str(cfg))
    assert _matched_texts(store, "the Acme brand") == ["Acme"]

    os.unlink(str(cfg))
    # Graceful: reloads to empty list rather than raising.
    store.maybe_reload()
    assert store.terms() == []
    assert store.detect("the Acme brand") == []


# ---------------------------------------------------------------------------
# detector adapter
# ---------------------------------------------------------------------------

def test_custom_term_detector_delegates_to_store(tmp_path):
    cfg = tmp_path / "terms.json"
    _write_config(cfg, ["Acme"])
    store = CustomTermsStore(path=str(cfg))
    detector = CustomTermDetector(store)
    assert detector.category == CATEGORY
    text = "the Acme brand"
    spans = detector.detect(text)
    assert len(spans) == 1
    assert text[spans[0].start:spans[0].end] == "Acme"
