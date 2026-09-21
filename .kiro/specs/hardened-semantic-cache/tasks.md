# Implementation Plan: Hardened Semantic Cache

Convert the feature design into a series of prompts for a code-generation LLM that will implement each step with incremental progress. Make sure that each prompt builds on the previous prompts, and ends with wiring things together. There should be no hanging or orphaned code that isn't integrated into a previous step. Focus ONLY on tasks that involve writing, modifying, or testing code.

## Overview

The plan builds the hardened semantic-cache stage bottom-up, mirroring the Phase 1 (pre-inference-redaction) module patterns closely: dependencies first, then the pure decision core and the embedding/vector-store loaders in `semantic_cache.py`, then the append-only `cache_log.py` (mirrors `audit_log.py`) and the thread-safe `cache_state.py` (mirrors `redaction_state.py`), then the offline stress-test dev tool, and finally the `main.py` generate-phase wiring (lookup after redaction, before compression; HIT short-circuit vs MISS store), the toggle endpoints, and the frontend `CacheReport` panel. Each task builds on the previous one and ends by integrating into the pipeline — no orphaned code.

Implementation language: **Python** (backend) and **TypeScript/React** (frontend) — taken directly from the design; no pseudocode.

**Test-priority policy (read before implementing):** Only two property tests are MANDATORY and are written as normal REQUIRED checkboxes (`- [ ]`, not starred): **Property 1 (Privacy)** and **Property 2 (Collision Resistance)**. They must not be deferred. Every other test sub-task — Properties 3–9, all unit tests, all FastAPI `TestClient` integration tests, all frontend component tests, and the stress-test determinism test — is OPTIONAL and marked with the starred `- [ ]*` convention (deferrable). See the Notes section.

## Tasks

- [ ] 1. Backend dependency setup
  - [ ] 1.1 Add semantic-cache dependencies to `backend/requirements.txt`
    - Add `sentence-transformers` (pinned, Python 3.13-compatible line) for local on-device embedding
    - Add `chromadb` (pinned, Python 3.13-compatible line) for the persistent local vector store
    - Add an install-time note for the `all-MiniLM-L6-v2` model artifact so it resolves from the local sentence-transformers / HF cache at runtime with no network call (installed/warmed at build time only, mirroring the `en_core_web_sm` note); `hypothesis` is already pinned from Phase 1
    - _Requirements: 1.4, 1.5, 2.1, 2.5, Non-Goals (nothing leaves the machine at runtime; artifacts fetched at build time)_

- [x] 2. Create semantic-cache decision core (`backend/semantic_cache.py`)
  - [x] 2.1 Define configuration defaults, `CacheDecision`, and the pure policy helpers
    - Module constants: `DEFAULT_TOP_K = 5`, `DEFAULT_MIN_SIMILARITY = 0.85`, `DEFAULT_MARGIN_THRESHOLD = 0.05`, `NO_RUNNER_UP_SENTINEL = 0.0`, `DEFAULT_CACHE_DIR`, `COLLECTION_NAME = "semantic_cache"`
    - `CacheDecision` frozen dataclass (`decision`, `top_score`, `runner_up_score`, `margin`, `latency_ms`, `entry_id`, `candidate_count`)
    - `hardened_decision(scores, *, min_similarity, margin_threshold) -> (decision, top_score, runner_up_score, margin)` — PURE, the single source of truth for the decision logic: descending scores; empty → miss; single candidate → runner-up sentinel `0.0`; exact top tie → margin forced `0.0`; else `margin = top - runner_up`; `hit` iff `top >= min_similarity AND margin >= margin_threshold`, every other case (below-min, ambiguous small/zero margin when `margin_threshold > 0`) → miss
    - `naive_decision(scores, *, min_similarity) -> str` — PURE baseline for the stress test: hit iff `top >= min_similarity`, ignoring the margin
    - No I/O, no network in this task
    - _Requirements: 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 5.4_

- [x] 3. Implement the embedding model loader (`backend/semantic_cache.py`)
  - [x] 3.1 Implement `load_embedding_model()` and `embed(model, text)`
    - `load_embedding_model()` lazily imports `sentence_transformers`, loads `all-MiniLM-L6-v2` with `device="cpu"`, returns the model or `None` on ANY failure (catch + WARNING), mirroring `redactor.load_ner_model`; no GPU, no network call at load time
    - `embed(model, text)` returns a deterministic vector via `encode(text, normalize_embeddings=True)` (unit-length for the cosine space); same text → identical vector; fixed dimensionality for the process
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8_

- [x] 4. Implement the vector store opener and hardened lookup (`backend/semantic_cache.py`)
  - [x] 4.1 Implement `open_vector_store()`
    - `chromadb.PersistentClient(path=DEFAULT_CACHE_DIR)` + `get_or_create_collection(COLLECTION_NAME, metadata={"hnsw:space": "cosine"})`; return the collection or `None` on ANY failure (catch + WARNING); persistent on-device only; no network call
    - _Requirements: 2.1, 2.3, 2.5, 2.6_

  - [x] 4.2 Implement `lookup(collection, embedding, *, top_k, min_similarity, margin_threshold) -> (CacheDecision, entry_or_None)`
    - Time from lookup start; `count() == 0` → miss (Req 3.10); `k = min(top_k, count())`; `collection.query(..., include=["distances","documents","metadatas"])`; convert cosine distance → similarity `clamp(1 - d, -1, 1)`; sort descending; delegate the decision to `hardened_decision`; build `CacheDecision`; return the Top_Match `{document, metadata}` ONLY on a hit, else `None`
    - _Requirements: 2.2, 3.1, 3.2, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 10.1_

- [ ] 5. Implement store-on-miss with upsert-on-near-duplicate (`backend/semantic_cache.py`)
  - [x] 5.1 Implement `store(collection, embedding, result_text, metadata) -> bool`
    - Before adding, run one internal `lookup()` of the just-produced embedding; if it would be a HIT against an existing entry, `update` that entry's document/metadata in place (same id); otherwise `add` a new `uuid4()` entry
    - `document` = the RESULT text (never the prompt); scalar-only metadata per the design schema (`task_mode`, `created_at`, `real_input_tokens`, `real_output_tokens`, `real_total_tokens`, `inference_time_ms`, `result_char_len`); never store the raw summary, raw prompt, or redacted-prompt text
    - Return `True` on success, `False` on ANY failure (catch + WARNING); never raise into the request path
    - _Requirements: 2.2, 2.4, 6.2, 6.3, 6.4, 6.8_

  - [ ]* 5.2 Write property test — hardened decision correctness across all score sets
    - **Property 3: Hardened decision correctness across all score sets**
    - **Validates: Requirements 3.1, 3.2, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10**
    - Hypothesis generators cover the partitions: empty list, single element, exact top ties, near-`Margin_Threshold` pairs, below-`Minimum_Similarity` tops; assert the runner-up/sentinel, tie→margin 0.0, and hit-iff rule against `hardened_decision`; `@settings(max_examples>=100)`
    - Tag: `# Feature: hardened-semantic-cache, Property 3: hardened decision correctness across all score sets`

  - [ ]* 5.3 Write property test — store-on-miss then identical prompt hits
    - **Property 5: Store-on-miss then an identical redacted prompt hits**
    - **Validates: Requirements 2.2, 6.2, 6.3, 7.6, 9.7**
    - Miss against the store, `store()` a non-empty result, then look up the SAME embedding → hit whose served document equals the stored result and whose derived tokens/time-saved equal the stored metadata; loads the real local model once, skips if the artifact is unavailable; `@settings(max_examples>=100)`
    - Tag: `# Feature: hardened-semantic-cache, Property 5: store-on-miss then identical prompt hits`

  - [ ]* 5.4 Write property test — embedding determinism and fixed dimensionality
    - **Property 8: Embedding is deterministic with fixed dimensionality**
    - **Validates: Requirements 1.3, 1.8**
    - For any two strings, both embeddings have identical length; the same string embedded twice is elementwise-identical; loads the real local model once, skips if unavailable; `@settings(max_examples>=100)`
    - Tag: `# Feature: hardened-semantic-cache, Property 8: embedding is deterministic with fixed dimensionality`

  - [ ]* 5.5 Write unit tests for `semantic_cache` loaders and lookup edge cases
    - `load_embedding_model` / `open_vector_store` return `None` on failure; `lookup` returns miss for zero/one/tie candidates; `store` returns `False` on a forced failure without raising; upsert-on-near-duplicate updates in place rather than adding
    - _Requirements: 1.6, 1.7, 2.6, 3.8, 3.9, 3.10, 6.8_

- [ ] 6. Implement the append-only cache decision log (`backend/cache_log.py`)
  - [x] 6.1 Implement `CacheLog` JSONL append writer mirroring `audit_log.py`
    - `DEFAULT_CACHE_LOG_PATH = backend/cache_decisions.jsonl`; public surface is ONLY the constructor, a read-only `path`, and `append(session_id, decision, *, top_score, runner_up_score, margin) -> bool`; no clear/delete/truncate/overwrite/rotate; file opened only in append mode under a `threading.Lock`
    - Entry: ISO-8601 `timestamp`, `session_id`, `decision`, `top_score`, `runner_up_score` (sentinel `0.0` when none), `margin`, and a scalar `had_runner_up` bool; no raw summary, raw prompt, redacted-prompt text, or sensitive value
    - On write failure catch, log WARNING, return `False`; a success returns `True`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [ ]* 6.2 Write property test — cache decision log is append-only
    - **Property 7: The cache decision log is append-only**
    - **Validates: Requirements 4.2, 4.5**
    - For any sequence of appends, every previously written line is preserved byte-for-byte and in order; assert the public surface exposes no clear/truncate/overwrite/rotate operation; `@settings(max_examples>=100)`
    - Tag: `# Feature: hardened-semantic-cache, Property 7: the cache decision log is append-only`

  - [ ]* 6.3 Write unit test for cache log append-failure path
    - Force an append error (unwritable path) and assert `append` returns `False`, does not raise, and exposes no raw value
    - _Requirements: 4.6_

- [ ] 7. Implement toggle and session state (`backend/cache_state.py`)
  - [x] 7.1 Implement `CacheState` mirroring `redaction_state.py`
    - `_enabled` default `True` (Req 8.6); `_cache_available` flag default `False` with `cache_available` property + `set_cache_available`; per-session `{hits, misses, tokensSaved, computeTimeSavedMs}`; all reads/writes under a `threading.Lock`
    - `is_enabled` / `set_enabled`; `record_decision(session_id, *, hit, tokens_saved=0, compute_ms_saved=0)`; `report_for(session_id) -> {hits, misses, decisions, hitRate, tokensSavedFromCache, computeTimeSavedMs}` (`hitRate = hits/decisions`, `0.0` when `decisions == 0`), a fresh JSON-serializable copy; `reset_session`; module-level singleton `state`
    - _Requirements: 1.6, 1.7, 2.6, 7.2, 7.3, 8.3, 8.4, 8.6_

  - [ ]* 7.2 Write property test — session accounting is consistent and non-negative
    - **Property 9: Session accounting is consistent and non-negative**
    - **Validates: Requirements 7.2, 7.3**
    - For any sequence of hit/miss records, `hitRate == hits/decisions` in `0.0..1.0` (0.0 at zero decisions); cumulative `tokensSavedFromCache` and `computeTimeSavedMs` are non-negative integers that never decrease; `@settings(max_examples>=100)`
    - Tag: `# Feature: hardened-semantic-cache, Property 9: session accounting is consistent and non-negative`

  - [ ]* 7.3 Write unit tests for `CacheState` defaults and availability gate
    - Default enabled on fresh start; `cache_available` defaults `False` and flips via `set_cache_available`; toggle get/set; `report_for` empty-session shape
    - _Requirements: 8.4, 8.6, 1.6, 2.6_

- [x] 8. Write the MANDATORY Privacy property test (Property 1) — REQUIRED, must not be deferred
  - [x] 8.1 Write property test — no raw or redacted-prompt content reaches any cache sink
    - **Property 1: No raw or redacted-prompt content ever reaches any cache sink**
    - **Validates: Requirements 1.2, 2.4, 4.3, 5.11, 6.4, 7.5, 9.2, 9.8**
    - **MANDATORY — this is a REQUIRED (non-optional) property test and MUST NOT be deferred.**
    - Hypothesis injects known secret tokens (SSNs, emails, API keys, names) into requirements summaries; after the cache stage runs on the redacted form, assert no raw sensitive value AND no `>= 4`-char substring of the redacted prompt appears in any stored Chroma **document**, any stored Chroma **metadata** value, any `cache_report` / `cache_benchmark` annotation, or any `cache_log` JSONL entry; assert the stored document is the RESULT text and the prompt is present only as the embedding vector + opaque id; run against the ACTUAL sinks (real Chroma docs/metadata, real annotation payloads, real log lines — not mocks); `@settings(max_examples>=100)`; loads the real local model once, skips if the artifact is unavailable
    - Tag: `# Feature: hardened-semantic-cache, Property 1: no raw or redacted-prompt content ever reaches any cache sink`

- [x] 9. Write the MANDATORY Collision-Resistance property test (Property 2) — REQUIRED, must not be deferred
  - [x] 9.1 Write property test — hardening never increases wrong hits on an adversarial set
    - **Property 2: Hardening never increases wrong hits on an adversarial set**
    - **Validates: Requirements 3.5, 3.7, 5.4, 5.5, 5.6**
    - **MANDATORY — this is a REQUIRED (non-optional) property test and MUST NOT be deferred.**
    - For any generated `Adversarial_Prompt_Set` (each prompt labeled with an intended-match group) and any valid config, assert the hardened cluster-based policy's `Wrong_Hit` count `<=` the naive single-threshold policy's `Wrong_Hit` count on the same set, model, and config; exercise the shared `hardened_decision` / `naive_decision` helpers; `@settings(max_examples>=100)`; loads the real local model once, skips if the artifact is unavailable
    - Tag: `# Feature: hardened-semantic-cache, Property 2: hardening never increases wrong hits on an adversarial set`

- [x] 10. Checkpoint — semantic-cache core complete
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 11. Implement the cache stress-test developer tool (`backend/cache_stress_test.py`)
  - [x] 11.1 Implement `load_adversarial_set`, `run_stress_test`, and the `__main__` CLI
    - `load_adversarial_set(path) -> list[{"prompt", "group"}]`: reject the set with a `ValueError` reporting the missing label when any prompt has no/empty group (Req 5.3); redact each prompt before embedding so no raw sensitive value reaches the report (Req 5.11)
    - `run_stress_test(prompt_set, *, model, top_k, min_similarity, margin_threshold) -> dict`: embed on-device, evaluate each prompt under `naive_decision` and `hardened_decision`, count a `Wrong_Hit` when a served entry's group differs from the query group; deterministic given the same set/config/model; empty set → zero prompts, `0.0` rates for both; returns `{promptsEvaluated, naiveWrongHits, naiveWrongHitRate, hardenedWrongHits, hardenedWrongHitRate}`
    - `if __name__ == "__main__":` a local CLI only — never a served endpoint; reuses the production `hardened_decision` / `naive_decision` / `load_embedding_model`; no network call
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10, 5.11_

  - [ ]* 11.2 Write stress-test determinism + validation unit tests
    - Run `run_stress_test` twice on the same generated set/config/model → identical counts and rates (Req 5.8); missing-group rejection reports the missing label (Req 5.3); empty set → zeros for both policies (Req 5.10); loads the real local model once, skips if unavailable
    - _Requirements: 5.3, 5.8, 5.10_

- [ ] 12. Wire the cache stage into the generate phase (`backend/main.py`)
  - [x] 12.1 Extend the lifespan handler to load the cache stage
    - Add module globals `embedding_model`, `vector_store`, `cache_log`; in `lifespan` call `load_embedding_model()` + `open_vector_store()` + construct `CacheLog()`, then set `cache_state.set_cache_available(embedding_model is not None and vector_store is not None)`; log a WARNING and continue when unavailable; no network call
    - _Requirements: 1.5, 1.6, 1.7, 2.1, 2.6_

  - [x] 12.2 Insert the cache lookup between redaction and compression (pre-stream)
    - Add closed-over generate-phase state (`gen_cache_enabled`, `gen_cache_hit`, `gen_cache_decision`, `gen_cache_entry`, `gen_query_embedding`); after `gen_redacted` exists and redaction is OK, compute `gen_cache_enabled = cache_state.is_enabled() and cache_state.cache_available`; when enabled, `embed(embedding_model, gen_redacted)` → lookup → `cache_log.append(...)`; set `gen_cache_hit`
    - On a HIT skip compression entirely (leave `gen_compressed_detail = None`); on a MISS (or cache disabled) run `compress_prompt_detailed` + `verify_placeholders` exactly as today; redaction failure still aborts pre-stream so the cache never runs
    - _Requirements: 4.1, 6.6, 8.1, 8.2, 9.1, 9.2, 9.3, 9.7, 9.9_

  - [x] 12.3 Emit the HIT-path frames in the streaming generator
    - When `gen_cache_hit`: `record_decision(hit=True, tokens_saved, compute_ms_saved)` from stored metadata, then yield in order — `compression_stats` (`skipped:true`, zeroed), `compression_diff` (`skipped:true`, `tokens:[]`), the existing `_redaction_annotations(...)` frames, `cache_report` (hit, this-request + cumulative savings), `cache_benchmark` (hit, lookup latency + savings), `task_mode` from metadata, a reconstructed `real_usage` (`perSubtask:[]`) from stored token counts, the cached `document` streamed via `0:` deltas in 16-char chunks, and `finish_message("stop")` LAST; skip compression, PERFORM, BUILD, and any store
    - _Requirements: 6.5, 7.1, 7.5, 7.6, 9.3, 9.4, 9.5, 9.6, 10.5_

  - [x] 12.4 Emit the MISS-path cache annotations and store the produced result
    - On a real MISS (`gen_cache_enabled and not gen_cache_hit`): `record_decision(hit=False)`, emit `cache_report` (`hit:false`) and `cache_benchmark` (`decision:"miss"`, zeros), then proceed to compression + PERFORM/BUILD unchanged; when the cache is disabled/unavailable emit the disabled `cache_report` variant (`stageEnabled:false`); wrap benchmark capture so a failure emits `cache_benchmark_unavailable` and continues
    - After a non-empty result is produced: time the produce step; PERFORM reads `usage` from `invoke_sync`; BUILD reads `realInputTokens`/`realOutputTokens`/`realTotalTokens` from the router's `real_usage` event in the `pending_events` buffer; build scalar metadata and call `store(vector_store, gen_query_embedding, result_text, metadata)`; skip the store when the result is empty
    - _Requirements: 6.1, 6.2, 6.3, 6.7, 6.8, 7.2, 8.5, 9.7, 10.2, 10.4, 10.5, 10.7_

  - [x] 12.5 Add helper builders for the cache annotation frames (`backend/main.py`)
    - Add `cache_report` (hit/miss + disabled variant), `cache_benchmark` (hit/miss), `cache_benchmark_unavailable`, and the skipped-compression `compression_stats`/`compression_diff` builders per the design annotation contract; cache-savings fields distinct from compression-savings fields; no raw values; used by tasks 12.3/12.4
    - _Requirements: 7.1, 7.4, 7.5, 8.5, 10.4, 10.5, 10.7_

  - [ ]* 12.6 Write property test — a hit short-circuits compression and inference
    - **Property 4: A hit short-circuits compression and inference**
    - **Validates: Requirements 6.5, 9.3, 9.4**
    - For any redacted prompt whose lookup is a hit, assert the request invokes neither compression, nor `invoke_sync`, nor `run_task_router`, creates no new `Cache_Entry`, and the served text equals the stored document; `@settings(max_examples>=100)`
    - Tag: `# Feature: hardened-semantic-cache, Property 4: a hit short-circuits compression and inference`

  - [ ]* 12.7 Write property test — a disabled stage performs no embed, lookup, store, or log
    - **Property 6: A disabled stage performs no embed, lookup, store, or log**
    - **Validates: Requirements 1.6, 1.7, 2.6, 8.2, 8.5**
    - With the cache disabled (toggled off or unavailable), assert no embedding, lookup, store, or cache-log append occurs, the request is passed to compression, and the emitted `cache_report` indicates the stage was disabled; `@settings(max_examples>=100)`
    - Tag: `# Feature: hardened-semantic-cache, Property 6: a disabled stage performs no embed, lookup, store, or log`

  - [ ]* 12.8 Write generate-stream integration tests (FastAPI `TestClient`)
    - HIT path: cached text arrives via `0:` deltas, `finish` last, `cache_report(hit)` + redaction annotations + `task_mode` precede it (Req 9.4, 9.5, 10.5); skipped compression annotation on a hit has `skipped:true` with all numeric fields present (Req 9.6); MISS path proceeds to compression + PERFORM/BUILD then stores (Req 9.7); missing `sessionId` → HTTP 400 before any embed/lookup (Req 9.9); startup fallbacks (model/store `None` → `cache_available == False`, requests miss-equivalent) (Req 1.6, 2.6); failure fallbacks (log-append, store, benchmark → `cache_benchmark_unavailable`) (Req 4.6, 6.8, 10.7)
    - _Requirements: 1.6, 2.6, 4.6, 6.7, 6.8, 8.5, 9.4, 9.5, 9.6, 9.7, 9.9, 10.7_

- [x] 13. Add the cache toggle FastAPI endpoints (`backend/main.py`)
  - [x] 13.1 Implement `GET`/`POST /api/cache/toggle`
    - `GET` → `{"enabled": cache_state.is_enabled()}` (Req 8.4); `POST` reuses the existing `ToggleBody`, calls `cache_state.set_enabled(...)`, returns `{"enabled": ...}`; new state applies to generate requests beginning after the change (in-flight requests keep the snapshot taken at entry); mirrors the redaction toggle endpoints
    - _Requirements: 8.3, 8.4, 8.6_

  - [ ]* 13.2 Write toggle endpoint integration tests (FastAPI `TestClient`)
    - `GET`/`POST` response shapes and status codes; default-enabled on fresh start
    - _Requirements: 8.3, 8.4, 8.6_

- [x] 14. Checkpoint — backend wiring complete
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 15. Build the Cache Report frontend panel
  - [x] 15.1 Create `CacheReport` component (`frontend/components/CacheReport.tsx`)
    - Right-pane card mirroring `RedactionReport.tsx`, visually separate from the compression-savings presentation (Req 7.4); header with a `stageEnabled ? "<hitRate>% hit" : "Stage off"` badge; disabled state note + zeros (Req 8.5); empty state when `decisions === 0` (Req 7.9); populated `StatBadge`s for Hit Rate, Tokens Saved from Cache, Compute Time Saved; a benchmark sub-block (lookup latency, decision, tokens saved, inference time saved) using the same `StatBadge` layout/labels/units as the redaction benchmark (Req 10.6)
    - Export `CacheReportData` and `CacheBenchmarkData` interfaces per the design
    - _Requirements: 7.4, 7.7, 7.9, 8.5, 10.6_

  - [x] 15.2 Wire cache annotations into the stream loop (`frontend/app/page.tsx`)
    - Add `cacheReport` / `cacheBenchmark` state, cleared in `triggerGenerate` and `resetChat` alongside the redaction panels; add session-guarded `processLine` handlers for `cache_report` and `cache_benchmark` (apply only when `payload.sessionId === sessionId`, ignore mismatches — Req 7.8) and `cache_benchmark_unavailable` (clears the benchmark); render `<CacheReport report={cacheReport} benchmark={cacheBenchmark} />` in the right pane near the Redaction Report
    - Add the backward-compatible compression `skipped` tolerance: extend `CompressionStats` with `skipped?: boolean`, map `skipped: payload.skipped ?? false`, and render a "skipped — served from cache" badge instead of the `Nx smaller` badge when `stats.skipped` (all numeric fields still present, so nothing crashes)
    - _Requirements: 7.7, 7.8, 7.9, 9.6, 10.6_

  - [x] 15.3 Add the cache on/off switch (`frontend/components/RedactionSettings.tsx` or a sibling `CacheSettings.tsx`)
    - Add a second toggle row that reads `GET /api/cache/toggle` on mount and flips via `POST /api/cache/toggle`, reusing the existing switch markup and error handling; requests target only the local backend
    - _Requirements: 8.3, 8.4_

  - [ ]* 15.4 Write component tests for `CacheReport` and the cache toggle
    - Renders hit rate / tokens saved / compute time saved on a matching session id (Req 7.7); ignores a mismatched session id (Req 7.8); shows the empty state at zero decisions (Req 7.9); renders the skipped-compression badge; the cache toggle reads and flips state
    - _Requirements: 7.7, 7.8, 7.9, 8.3, 8.4, 9.6_

- [x] 16. Final checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- **Mandatory-test policy (explicit):** ONLY **Property 1 (Privacy, task 8.1)** and **Property 2 (Collision Resistance, task 9.1)** are mandatory. They are written as normal REQUIRED checkboxes (`- [ ]`, not starred) and MUST NOT be deferred. EVERY other test sub-task — Properties 3, 4, 5, 6, 7, 8, 9; all unit tests; all FastAPI `TestClient` integration tests; all frontend component tests; and the stress-test determinism test — is OPTIONAL and marked with the starred `- [ ]*` convention, and may be deferred for a faster MVP.
- Tasks marked with `*` are optional and will NOT be implemented by the code-generation agent; unstarred sub-tasks (including tasks 8.1 and 9.1) MUST be implemented.
- Each of the 9 correctness properties is implemented by exactly one property-based test (Hypothesis, `@settings(max_examples>=100)`), tagged `# Feature: hardened-semantic-cache, Property N: ...`, and placed close to the code it validates.
- Property 1 asserts the "no raw value AND no length-≥4 substring of the redacted prompt" invariant against the ACTUAL Chroma documents/metadata, the `cache_report`/`cache_benchmark` payloads, and the JSONL log lines — never mocks — so any regression that leaks prompt text into a sink fails the build.
- Embedding-dependent tests (Properties 1, 2, 5, 8, and the stress-test determinism test) load the real local `all-MiniLM-L6-v2` model once per session and skip (never fail) when the artifact is unavailable, consistent with the graceful-fallback design.
- Each task references specific requirements (and property numbers where relevant) for traceability.
- Pipeline placement (lookup after redaction, before compression), HIT short-circuit vs MISS store, the redacted-prompt-only privacy constraint, the append-only log, and the scalar-only Chroma metadata schema follow the design exactly.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "6.1", "7.1"] },
    { "id": 1, "tasks": ["3.1", "6.2", "6.3", "7.2", "7.3"] },
    { "id": 2, "tasks": ["4.1", "5.4"] },
    { "id": 3, "tasks": ["4.2"] },
    { "id": 4, "tasks": ["5.1", "5.2"] },
    { "id": 5, "tasks": ["5.3", "5.5", "8.1", "9.1", "11.1"] },
    { "id": 6, "tasks": ["11.2", "12.1"] },
    { "id": 7, "tasks": ["12.2"] },
    { "id": 8, "tasks": ["12.5"] },
    { "id": 9, "tasks": ["12.3", "12.4"] },
    { "id": 10, "tasks": ["12.6", "12.7", "12.8", "13.1"] },
    { "id": 11, "tasks": ["13.2", "15.1", "15.3"] },
    { "id": 12, "tasks": ["15.2"] },
    { "id": 13, "tasks": ["15.4"] }
  ]
}
```
