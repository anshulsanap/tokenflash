# Implementation Plan: Pre-Inference Redaction

Convert the feature design into a series of prompts for a code-generation LLM that will implement each step with incremental progress. Make sure that each prompt builds on the previous prompts, and ends with wiring things together. There should be no hanging or orphaned code that isn't integrated into a previous step. Focus ONLY on tasks that involve writing, modifying, or testing code.

## Overview

The plan builds the redaction stage bottom-up: dependencies first, then the core `redactor.py` primitives (Span/Detector/RedactionResult, placeholder mapping), then each detector, then the supporting stores (custom terms, audit log, toggle/session state). Next the `redact()` orchestration ties the detectors together, the compressor is hardened to preserve placeholders, and finally everything is wired into the `main.py` generate phase and exposed through FastAPI endpoints and the frontend. Property-based tests (Hypothesis, one test per correctness property, ≥100 iterations) and example/integration tests are placed close to the code they validate so regressions surface early.

Implementation language: **Python** (backend) and **TypeScript/React** (frontend) — taken directly from the design; no pseudocode.

## Tasks

- [x] 1. Backend dependency setup
  - [x] 1.1 Add redaction dependencies to `backend/requirements.txt`
    - Add `spacy` (pinned) for the local NER model
    - Add the `en_core_web_sm` local model artifact requirement (pinned wheel URL or install note) so NER loads from a local artifact, no network call at request time
    - Add `hypothesis` (pinned) for property-based tests
    - _Requirements: 3.3, 3.4, Non-Goals (nothing leaves the machine)_

- [x] 2. Create redactor core primitives (`backend/redactor.py`)
  - [x] 2.1 Define `Span` dataclass, `Detector` Protocol, and `RedactionResult` dataclass
    - `Span(start, end, category)` frozen, offsets into original text, no raw value stored
    - `Detector` Protocol with `category` attr and pure `detect(text) -> list[Span]`
    - `RedactionResult(redacted_text, redactions, category_counts, chars_redacted, latency_ms, ok=True)`
    - _Requirements: 1.3, 1.4, 6.3, 7.3_

  - [x] 2.2 Implement placeholder format and category ↔ placeholder mapping
    - `⟦REDACTED_<CATEGORY>⟧` format using U+27E6/U+27E7 delimiters (no `{ } [ ] : , "` or whitespace)
    - Single dict mapping each category to its placeholder and a `PLACEHOLDER_RE = re.compile(r"⟦REDACTED_[A-Z_]+⟧")`
    - Helper to build a placeholder from a category and to parse a placeholder back to a category
    - _Requirements: 1.3, 11.4_

  - [x] 2.3 Implement `verify_placeholders(redacted_summary, compressed_output) -> bool`
    - Extract placeholders from both strings via `PLACEHOLDER_RE`; return True iff the multiset (count + label set) is identical
    - _Requirements: 11.1, 11.3, 11.5_

- [ ] 3. Implement built-in regex detectors (`backend/redactor.py`)
  - [x] 3.1 Implement the six structured-secret detectors
    - `ssn`, `credit_card` (digit-run candidates validated by Luhn to cut false positives), `email`, `phone`, `api_key` (`sk-…`, `AKIA…`, `ghp_…`, `Bearer …`)
    - Compile all patterns once at import; each `detect` returns all matches in one pass; pure, no network
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [ ]* 3.2 Write property test — matched values redacted with correct placeholder and count
    - **Property 7: Matched values are redacted with the correct placeholder and count**
    - **Validates: Requirements 2.7, 4.2**
    - Hypothesis strategies generate valid values per category (Luhn-valid cards, emails, phones, `sk-`/`AKIA`/`ghp_`/`Bearer` keys) interleaved with prose; `@settings(max_examples>=100)`
    - Tag: `# Feature: pre-inference-redaction, Property 7: ...`

  - [x]* 3.3 Write unit tests for regex detectors
    - Positive/negative cases per category, Luhn rejection of invalid cards, no-match returns input unchanged
    - _Requirements: 2.6, 2.9_

- [x] 4. Implement NER detector with startup load and graceful fallback (`backend/redactor.py`)
  - [x] 4.1 Implement `NerDetector` and local model load helper
    - `NerDetector(nlp_or_none)` returns `person` spans for `PERSON`/`ORG`/`GPE` entities; returns `[]` when `nlp is None`
    - Local-artifact load helper used by the FastAPI startup handler; on failure set `ner_available=False`, log "NER disabled", omit detector; no network call
    - _Requirements: 3.1, 3.3, 3.4, 3.5_

  - [x]* 4.2 Write unit tests for NER detector and fallback
    - Representative person-name sentences produce `person` spans; `nlp=None` yields no spans and emits no `person` redactions
    - _Requirements: 3.1, 3.5_

- [ ] 5. Implement custom-terms store (`backend/custom_terms.py`)
  - [x] 5.1 Implement thread-safe store with load/validate and matcher
    - JSON array config at `backend/redaction_terms.json`; load up to 10,000 terms, each 1–256 chars; skip empty/>256/case-insensitive duplicates and record a skip reason; missing/unreadable → empty list + recorded reason
    - Maintain lowercased set + compiled alternation regex under a `threading.Lock`; build a `CustomTermDetector` snapshot that matches full terms case-insensitively (word-boundary aware) emitting `custom_term` spans
    - _Requirements: 4.1, 4.2, 4.3, 4.9_

  - [x] 5.2 Implement add/remove with atomic persistence and mtime live reload
    - Add (trim, reject empty-after-trim/>256, case-insensitive dedupe) and remove (case-insensitive) mutate under the lock, then persist via temp-file + `os.replace`
    - On persistence failure retain in-memory list, leave prior file bytes unchanged, surface the error
    - Track config mtime; reload under the lock when mtime changed (checked at request entry) so changes apply to requests beginning >2s later
    - _Requirements: 4.4, 4.5, 4.6, 4.7, 4.8_

  - [ ]* 5.3 Write property test — custom-term loading respects validation bounds
    - **Property 12: Custom-term loading respects validation bounds**
    - **Validates: Requirements 4.1, 4.3**
    - Strategy generates candidate lists mixing valid, empty, >256-char, and duplicate terms; assert loaded set is exactly the valid unique terms (≤10,000) and every skip is recorded; `@settings(max_examples>=100)`
    - Tag: `# Feature: pre-inference-redaction, Property 12: ...`

  - [x]* 5.4 Write unit tests for store add/remove/reload/persistence
    - Round-trip persistence, duplicate no-op, atomic-write failure retains in-memory list and prior file, mtime reload semantics
    - _Requirements: 4.4, 4.5, 4.6, 4.7, 4.8, 4.9_

- [x] 6. Implement append-only audit log (`backend/audit_log.py`)
  - [x] 6.1 Implement JSONL append writer with failure handling
    - `append(category, session_id)` writes one JSON object per line (`timestamp` ISO-8601, `category`, `session_id`) to `backend/redaction_audit.jsonl` in append mode and flushes; never rewrites prior lines; never stores raw value or ≥4-char substring
    - On append failure catch, continue, record to app log (not the audit file) that the entry could not be written, without exposing the raw value
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x]* 6.2 Write property test — audit log never contains raw values
    - **Property 5: Audit log never contains raw values**
    - **Validates: Requirements 6.1, 6.3**
    - Slide a 4-char window over each original secret and assert none appears in any audit line; assert each entry has ISO-8601 timestamp, category, session id; `@settings(max_examples>=100)`
    - Tag: `# Feature: pre-inference-redaction, Property 5: ...`

  - [x]* 6.3 Write property test — one entry per redaction, append-only
    - **Property 11: One audit entry per redaction, append-only**
    - **Validates: Requirements 6.1, 6.4**
    - Redact k spans → exactly k new entries appended; previously written entries remain byte-for-byte unchanged; `@settings(max_examples>=100)`
    - Tag: `# Feature: pre-inference-redaction, Property 11: ...`

  - [x]* 6.4 Write unit test for audit append failure path
    - Force an append error (read-only/failing file) and assert redaction continues and no raw value is exposed
    - _Requirements: 6.5_

- [ ] 7. Implement toggle and session state (`backend/redaction_state.py`)
  - [x] 7.1 Implement `RedactionState` with default-enabled toggle and cumulative session counts
    - `enabled` defaults True; `session_counts: dict[session_id -> dict[category -> cumulative count]]`; guarded reads/writes; helpers to get/set enabled and increment per-session category counts and read cumulative counts
    - `ner_available` flag surfaced for the NER fallback
    - _Requirements: 3.5, 7.2, 8.4, 8.6_

  - [ ]* 7.2 Write unit tests for state defaults and count accumulation
    - Default enabled on fresh start; per-session cumulative increments; toggle get/set
    - _Requirements: 8.4, 8.6, 7.2_

- [ ] 8. Implement `redact()` orchestration (`backend/redactor.py`)
  - [x] 8.1 Implement overlap resolution by fixed precedence
    - Precedence (highest first): `ssn, credit_card, api_key, email, phone, custom_term, person`; produce non-overlapping spans where each character is redacted once under the earliest-matched category; no duplicate `person` over characters claimed by another detector
    - _Requirements: 2.8, 3.2_

  - [x] 8.2 Implement `redact(text, session_id) -> RedactionResult` right-to-left splice
    - Run active detectors, resolve overlaps, splice spans right-to-left substituting placeholders, copy non-span chars byte-for-byte; append audit entry + increment session counts per replaced span; compute per-category counts, chars redacted, and `latency_ms`; return unchanged text when no spans; set `ok=False` and short-circuit on detector error (Req 1.7)
    - _Requirements: 1.3, 1.4, 1.6, 2.9, 9.1, 9.2, 9.3_

  - [x]* 8.3 Write property test — no sensitive value leaks downstream
    - **Property 1: No sensitive value leaks downstream**
    - **Validates: Requirements 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 7.3**
    - Slide a 4-char window over each detected secret; assert no window appears in redacted text or any annotation payload derived from it; `@settings(max_examples>=100)`
    - Tag: `# Feature: pre-inference-redaction, Property 1: ...`

  - [ ]* 8.4 Write property test — redaction is idempotent
    - **Property 2: Redaction is idempotent**
    - **Validates: Requirements 1.3, 1.4**
    - Redacting already-redacted output yields the same placeholder set with no new redactions; `@settings(max_examples>=100)`
    - Tag: `# Feature: pre-inference-redaction, Property 2: ...`

  - [ ]* 8.5 Write property test — non-sensitive text preserved byte-for-byte
    - **Property 4: Non-sensitive text is preserved byte-for-byte**
    - **Validates: Requirements 1.4, 1.6, 2.9**
    - Every char outside a detected span is present, unchanged, same order; no-span text returned unchanged; `@settings(max_examples>=100)`
    - Tag: `# Feature: pre-inference-redaction, Property 4: ...`

  - [x]* 8.6 Write property test — overlaps resolve to single highest-precedence category
    - **Property 8: Overlaps resolve to the single highest-precedence category**
    - **Validates: Requirements 2.8, 3.2**
    - Random overlapping span sets resolve to non-overlapping redactions each under the earliest-matched category, no duplicate `person`; `@settings(max_examples>=100)`
    - Tag: `# Feature: pre-inference-redaction, Property 8: ...`

  - [ ]* 8.7 Write property test — benchmark values non-negative and complete
    - **Property 9: Benchmark values are non-negative and complete**
    - **Validates: Requirements 9.1, 9.2, 9.3, 9.6**
    - `latencyMs>=0`, `charsRedacted>=0` equals sum of span lengths, per-category integer `>=0` for every built-in category (defaulting 0), all-zero when nothing redacted; `@settings(max_examples>=100)`
    - Tag: `# Feature: pre-inference-redaction, Property 9: ...`

  - [ ]* 8.8 Write unit test — detector error triggers redaction failure
    - Inject a raising detector; assert `redact` returns `ok=False` and no partial redacted text leaks
    - _Requirements: 1.7_

- [ ] 9. Checkpoint — redaction core complete
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 10. Harden compressor to preserve placeholders (`backend/compressor.py`)
  - [x] 10.1 Add `PLACEHOLDER_RE` guard and force-preserve in scoring
    - Define `PLACEHOLDER_RE = re.compile(r"⟦REDACTED_[A-Z_]+⟧")`; in `_score_and_tag_tokens`, after scoring force `preserve=True` for any token fully matching it; never split/merge/discard placeholder tokens
    - _Requirements: 11.1, 11.2, 11.4_

  - [x]* 10.2 Write property test — placeholders invariant through compression
    - **Property 3: Placeholders are invariant through compression**
    - **Validates: Requirements 11.1, 11.2, 11.3, 11.4**
    - Generate redacted texts with placeholders in varied positions; assert placeholder multiset and byte-for-byte characters identical in compressor output; `@settings(max_examples>=100)`
    - The Hypothesis generator MUST include a case with a repeated sensitive value producing ≥2 identical placeholders, asserting ALL duplicates survive compression (locks in that the compressor performs no content-level dedup and the multiset check does not false-positive)
    - _Property 3 / Req 11.3_
    - Tag: `# Feature: pre-inference-redaction, Property 3: ...`

  - [x]* 10.3 Write placeholder-preservation regression test
    - Run each placeholder through `compress_prompt_detailed` and assert byte-for-byte survival (locks Option A format against future compressor edits)
    - Add a repeated-value case asserting both/all identical placeholders survive byte-for-byte through `compress_prompt_detailed`
    - _Requirements: 11.1, 11.3, 11.4_

- [ ] 11. Wire redaction into the generate phase (`backend/main.py`)
  - [x] 11.1 Acquire required session id and insert redaction stage before compression
    - Read `sessionId` from the request body; treat a MISSING or EMPTY `sessionId` as a CLIENT ERROR — reject it and do NOT run redaction with an ephemeral/per-request id (never invent one via `uuid.uuid4()`). On the streaming `generate` path emit a `redaction_failure` annotation with reason `missing_session_id` and end with the finish frame without invoking `compress_prompt_detailed` on the unredacted summary; for a non-streaming rejection return HTTP 400 with a descriptive body
    - Only for a valid (present, non-empty) `sessionId`: run redaction between `extract_requirements_summary` and `compress_prompt_detailed` when the toggle is enabled; feed only the redacted summary to compression; live-reload custom terms at request entry
    - _Requirements: 1.1, 1.2, 7.2, 8.1_

  - [x] 11.2 Verify placeholders and enforce fail-closed behavior
    - After compression call `verify_placeholders(redacted_summary, compressed)`; on redaction failure or placeholder corruption block/suppress the unredacted/corrupted text from every downstream step and annotation and emit `redaction_failure` with a reason; end with the finish frame
    - _Requirements: 1.5, 1.7, 11.5_

  - [x] 11.3 Emit redaction annotations before the finish frame
    - Emit `redaction_report` (session id, `stageEnabled`, per-category cumulative `counts`, `totalRedactions`) and `redaction_benchmark` (session id, `stageEnabled`, `latencyMs`, `charsRedacted`, `perCategoryCounts` defaulting every built-in category to 0) via `data_annotation` before `finish_message`; on a benchmark capture failure emit `redaction_benchmark_unavailable` and continue
    - _Requirements: 7.1, 7.2, 7.3, 9.2, 9.3, 9.4, 9.6, 9.7_

  - [x] 11.4 Implement disabled-stage pass-through
    - When disabled, pass the summary byte-for-byte unchanged with no scan, redaction, or audit append, and emit a `redaction_report` with `stageEnabled:false`, `counts:{}`, `totalRedactions:0`
    - _Requirements: 8.2, 8.5_

  - [x] 11.5 Load NER model in a FastAPI startup handler
    - Attempt the local-artifact load once at startup before the first generate request; on failure set `ner_available=False` and continue with regex + custom-term detectors
    - _Requirements: 3.4, 3.5_

  - [ ]* 11.6 Write property test — disabled stage passes text through byte-for-byte
    - **Property 6: Disabled stage passes text through byte-for-byte**
    - **Validates: Requirements 8.2**
    - With the stage disabled, output equals input byte-for-byte, no scan/redaction, no audit append; `@settings(max_examples>=100)`
    - Tag: `# Feature: pre-inference-redaction, Property 6: ...`

  - [ ]* 11.7 Write property test — session redaction counts are cumulative
    - **Property 10: Session redaction counts are cumulative**
    - **Validates: Requirements 7.2**
    - Over a sequence of generate requests in one session, each `redaction_report` category count equals cumulative redactions for that category; `@settings(max_examples>=100)`
    - Tag: `# Feature: pre-inference-redaction, Property 10: ...`

  - [ ]* 11.8 Write generate-stream integration tests (FastAPI TestClient)
    - Assert `redaction_report`/`redaction_benchmark` appear before the `d:` finish frame; `compression_stats`/`compression_diff` reflect redacted text; `redaction_failure` path suppresses output; `redaction_benchmark_unavailable` path continues
    - Assert a generate request with a missing/empty `sessionId` is rejected via the `missing_session_id` path and does NOT emit compression/redaction annotations for an ephemeral id (no redaction runs on an invented per-request id)
    - _Requirements: 1.5, 1.7, 7.2, 9.4, 9.7, 11.5_

- [ ] 12. Add custom-terms and toggle FastAPI endpoints (`backend/main.py`)
  - [x] 12.1 Implement custom-terms endpoints
    - `GET /api/redaction/terms` → `{"terms": [...]}` (empty when none); `POST` trims, rejects empty-after-trim/>256 with 400, case-insensitive duplicate returns existing list, else returns updated list; `DELETE` removes case-insensitively returning `removed` bool; persistence failure returns descriptive 500 with in-memory list retained
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

  - [x] 12.2 Implement toggle endpoints
    - `GET /api/redaction/toggle` → `{"enabled": bool}`; `POST` sets state applying to generate requests beginning after the change, in-flight requests keep prior state
    - _Requirements: 8.3, 8.4_

  - [ ]* 12.3 Write endpoint integration tests (FastAPI TestClient)
    - GET/POST/DELETE terms (empty list, add, duplicate no-op, remove present/absent, validation 400, persistence 500) and GET/POST toggle response shapes and status
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 8.3, 8.4_

- [ ] 13. Checkpoint — backend complete
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 14. Build the Redaction Report frontend panel
  - [x] 14.1 Create `RedactionReport` component (`frontend/components/RedactionReport.tsx`)
    - Right-pane card matching Token Compression Report styling; one row per category with count (reusing `StatBadge`); benchmark figures (latency ms, chars redacted, per-category counts) with the same labels/units/layout as compression stats; empty-state message at zero redactions; disabled-state indication with zeros
    - _Requirements: 7.4, 7.6, 8.5, 9.5_

  - [x] 14.2 Wire redaction annotations into the stream loop (`frontend/app/page.tsx`)
    - Sending a stable `sessionId` on every generate request is a HARD REQUIREMENT (the backend now rejects requests without one): generate the id once per chat session via `crypto.randomUUID()` held in React state, reset only by `resetChat`, and send that same id on every generate request for the life of the session
    - Add `processLine` handlers for `redaction_report` and `redaction_benchmark` that update state only when the annotation `sessionId` matches the current session (ignore mismatches); add a `redaction_failure` handler that surfaces an error banner and clears the redaction panels; render `RedactionReport` in the right pane
    - _Requirements: 7.1, 7.2, 7.4, 7.5, 8.5, 9.4_

  - [ ]* 14.3 Write component tests for `RedactionReport`
    - Renders per-category counts on matching session id, ignores mismatched session id, shows empty state at zero redactions, presents benchmark numbers with compression-stats layout/units
    - _Requirements: 7.4, 7.5, 7.6, 9.5_

- [ ] 15. Build the Custom-Terms settings screen
  - [x] 15.1 Create `RedactionSettings` component (`frontend/components/RedactionSettings.tsx`)
    - On open GET `/api/redaction/terms` and render the list; on load failure show an error and do not render a partial/empty list as current terms; add (POST) and remove (DELETE) with pending indicator, success re-render from returned list, and error retains previously displayed list
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6_

  - [x] 15.2 Mount the settings screen in the frontend
    - Add an entry point in `frontend/app/page.tsx` (e.g. a settings toggle/section in the right pane) that renders `RedactionSettings`; requests target only the local backend
    - _Requirements: 10.1_

  - [ ]* 15.3 Write component tests for `RedactionSettings`
    - Load/add/remove flows with pending and error states, load-failure error without partial list
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6_

- [ ] 16. Final checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core implementation tasks are never optional.
- Each of the 12 correctness properties is implemented by exactly one property-based test (Hypothesis, `@settings(max_examples>=100)`), tagged `# Feature: pre-inference-redaction, Property N: ...`, and placed close to the code it validates.
- Privacy properties (P1, P5) assert the "no length-4 window of any secret appears" invariant.
- Each task references specific requirements (and properties where relevant) for traceability.
- Redaction stage placement, placeholder format (Option A), and fail-closed behavior follow the design exactly.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1"] },
    { "id": 1, "tasks": ["2.2", "5.1", "6.1", "7.1"] },
    { "id": 2, "tasks": ["2.3", "3.1", "4.1", "5.2", "6.2", "6.3", "6.4", "7.2"] },
    { "id": 3, "tasks": ["3.2", "3.3", "4.2", "5.3", "5.4", "8.1"] },
    { "id": 4, "tasks": ["8.2", "10.1"] },
    { "id": 5, "tasks": ["8.3", "8.4", "8.5", "8.6", "8.7", "8.8", "10.2", "10.3"] },
    { "id": 6, "tasks": ["11.1"] },
    { "id": 7, "tasks": ["11.2", "11.3", "11.4", "11.5"] },
    { "id": 8, "tasks": ["12.1", "12.2", "11.6", "11.7", "11.8"] },
    { "id": 9, "tasks": ["12.3", "14.1", "15.1"] },
    { "id": 10, "tasks": ["14.2", "15.2"] },
    { "id": 11, "tasks": ["14.3", "15.3"] }
  ]
}
```
