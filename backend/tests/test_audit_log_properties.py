"""Property and unit tests for the append-only audit log (`audit_log.py`).

Covers tasks 6.2, 6.3, and 6.4 of the pre-inference-redaction spec:

  * Property 5  — Audit log never contains raw values (Req 6.1, 6.3)
  * Property 11 — One audit entry per redaction, append-only (Req 6.1, 6.4)
  * Unit test   — append failure path continues gracefully (Req 6.5)

Every test points the ``AuditLog`` at a ``tmp_path`` file so the real
``backend/redaction_audit.jsonl`` is never touched. The audit module accepts
only an int ``length`` (never the raw value), so the raw value physically
cannot enter it; the Property 5 test proves this by feeding real secrets to
``len()`` only and then asserting no 4-char window of any secret appears in any
redaction-derived stored value of the written entry.
"""

from __future__ import annotations

import json
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from audit_log import AuditLog
from redactor import CATEGORIES, PLACEHOLDERS


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read_lines(path):
    """Read all non-empty lines from the audit file, or [] if absent."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return [ln for ln in handle.read().splitlines() if ln]
    except FileNotFoundError:
        return []


def _windows(value, size=4):
    """Yield every substring of length ``size`` (a sliding window)."""
    for i in range(len(value) - size + 1):
        yield value[i : i + size]


# Strategy: raw sensitive values shaped like real secrets — arbitrary text,
# long digit runs (cards/ssn/phone), email-ish and key-ish shapes.
_raw_values = st.one_of(
    st.text(min_size=1, max_size=64),
    st.from_regex(r"\d{9,19}", fullmatch=True),
    st.from_regex(r"[a-z]{3,10}@[a-z]{3,8}\.[a-z]{2,4}", fullmatch=True),
    st.from_regex(r"sk-[A-Za-z0-9]{16,40}", fullmatch=True),
)

# Session ids are UUID-shaped (hex + hyphens). The session id is legitimate
# metadata the caller supplies and is independently random, so a coincidental
# collision between it and an independently-drawn raw value is a test artifact,
# not a leak. The Property 5 check below excludes the session id (and the
# timestamp) from its value scan for exactly this reason, so the 4-char-window
# assertion only fires on a genuine raw-value leak into a redaction-derived field.
_session_ids = st.uuids().map(str)

_categories = st.sampled_from(list(CATEGORIES))


# ---------------------------------------------------------------------------
# Property 5 — audit log never contains raw values
# ---------------------------------------------------------------------------

# Feature: pre-inference-redaction, Property 5: Audit log never contains raw values
@settings(max_examples=200)
@given(raw_value=_raw_values, session_id=_session_ids, category=_categories)
def test_property_5_audit_log_never_contains_raw_values(raw_value, session_id, category):
    """No ≥4-char window of any redacted value appears in the audit file, and
    each entry carries an ISO-8601 timestamp, session id, category, placeholder
    and an integer length (never the raw value). Validates Req 6.1, 6.3.

    A fresh temp file is created per generated example (not a function-scoped
    fixture, which Hypothesis does not reset between examples).
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audit.jsonl"
        log = AuditLog(path=str(path))

        # The raw value is fed ONLY to len(); the audit module never sees it.
        assert log.append(
            category,
            session_id,
            length=len(raw_value),
            placeholder=PLACEHOLDERS[category],
        ) is True

        lines = _read_lines(log.path)
        assert len(lines) == 1
        entry = json.loads(lines[0])  # must be valid JSON

        # Metadata present and correct.
        assert entry["session_id"] == session_id
        assert entry["category"] == category
        assert entry["placeholder"] == PLACEHOLDERS[category]
        assert isinstance(entry["length"], int)
        assert entry["length"] == len(raw_value)
        # Timestamp parses as ISO-8601.
        datetime.fromisoformat(entry["timestamp"])
        # The entry stores ONLY these metadata fields — there is no field that
        # carries the raw sensitive value. Locking the schema means no raw field
        # can be silently added without this test noticing.
        assert set(entry.keys()) == {
            "timestamp",
            "session_id",
            "category",
            "placeholder",
            "length",
        }

        # Core no-leak proof: slide a 4-char window over the raw value; NONE may
        # appear in any STORED VALUE that could plausibly carry the secret.
        #
        # The old formulation scanned the whole JSON *text*, which produced a
        # false positive when a 4-char window of the raw value coincided with a
        # JSON structural token — e.g. raw_value 'aaa@aaa.tamp' shares 'tamp'
        # with the field NAME '"timestamp"'. Field names and punctuation are not
        # leaks. The real guarantee is that no secret content lands in the
        # entry's VALUES.
        #
        # Two entry values are legitimately independent of the raw value and are
        # allowed to coincide with it: the caller-supplied ``session_id`` (an
        # independently random UUID) and the ``timestamp`` (a time string). We
        # exclude only those and check EVERY OTHER field's value. This proves no
        # secret content is stored anywhere — and because the check spans all
        # remaining values (not a hard-coded field list), a raw value stored in
        # any newly added field would still be caught.
        checked_values = {
            key: value
            for key, value in entry.items()
            if key not in ("session_id", "timestamp")
        }
        haystack = "\x00".join(str(value) for value in checked_values.values())
        for window in _windows(raw_value, 4):
            assert window not in haystack, (
                f"4-char window {window!r} of the raw value leaked into a "
                f"redaction-derived audit field: {checked_values!r}"
            )


# ---------------------------------------------------------------------------
# Property 11 — one entry per redaction, append-only
# ---------------------------------------------------------------------------

# Feature: pre-inference-redaction, Property 11: One audit entry per redaction, append-only
@settings(max_examples=100)
@given(
    first=st.lists(st.tuples(_categories, _session_ids, st.integers(0, 512)), max_size=12),
    second=st.lists(st.tuples(_categories, _session_ids, st.integers(0, 512)), max_size=12),
)
def test_property_11_one_entry_per_redaction_append_only(first, second):
    """Appending k entries yields exactly k complete JSON lines, and appending
    more entries preserves every prior line byte-for-byte. Validates Req 6.1, 6.4.

    A fresh temp file is created per generated example.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audit.jsonl"
        log = AuditLog(path=str(path))

        # Append the first batch: exactly len(first) new lines, each valid JSON.
        for category, session_id, length in first:
            assert log.append(category, session_id, length=length) is True

        after_first = path.read_bytes() if first else b""
        lines_after_first = _read_lines(path)
        assert len(lines_after_first) == len(first)
        for line in lines_after_first:
            json.loads(line)  # every line is a complete JSON object

        # Append the second batch.
        for category, session_id, length in second:
            assert log.append(category, session_id, length=length) is True

        final_bytes = path.read_bytes() if (first or second) else b""
        lines_final = _read_lines(path)

        # Exactly k1 + k2 lines total.
        assert len(lines_final) == len(first) + len(second)
        for line in lines_final:
            json.loads(line)

        # Append-only: the earlier bytes are an unchanged prefix of the final file.
        assert final_bytes[: len(after_first)] == after_first


# Feature: pre-inference-redaction, Property 11: One audit entry per redaction, append-only
def test_property_11_concurrent_appends_no_interleaving(tmp_path):
    """N concurrent appends from many threads produce exactly N complete,
    non-interleaved JSON lines (the threading.Lock prevents partial lines)."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path=str(path))

    n_threads = 16
    per_thread = 25
    total = n_threads * per_thread

    def worker(tid):
        for i in range(per_thread):
            log.append("email", f"session-{tid}", length=(tid * 100 + i))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = _read_lines(path)
    assert len(lines) == total
    # Every line is a complete, well-formed JSON object (no corruption/interleave).
    for line in lines:
        entry = json.loads(line)
        assert entry["category"] == "email"
        assert isinstance(entry["length"], int)


# ---------------------------------------------------------------------------
# Task 6.4 — append failure path continues gracefully (Req 6.5)
# ---------------------------------------------------------------------------

def test_append_failure_returns_false_and_does_not_raise(tmp_path):
    """Pointing the log at a path inside a non-existent directory makes the
    open() fail; append must swallow it, return False, and never raise so the
    redaction flow continues. No raw value is exposed (there is none)."""
    unwritable = tmp_path / "does-not-exist" / "nested" / "audit.jsonl"
    log = AuditLog(path=str(unwritable))

    result = log.append("credit_card", "session-x", length=16)

    assert result is False
    # Nothing was written and the redaction flow can continue.
    assert not unwritable.exists()


def test_append_failure_via_monkeypatched_open(tmp_path, monkeypatch):
    """Force open() itself to raise and assert append degrades gracefully."""
    log = AuditLog(path=str(tmp_path / "audit.jsonl"))

    import builtins

    def _boom(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(builtins, "open", _boom)

    # Must not propagate; returns the failure indicator.
    assert log.append("person", "session-y", length=8) is False
