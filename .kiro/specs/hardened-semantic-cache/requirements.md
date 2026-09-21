# Requirements Document

## Introduction

TokenQuick adds a **hardened semantic caching** stage to its FastAPI backend. The stage sits in the `/api/chat` "generate" phase, **after** the existing pre-inference redaction stage and **before** compression and local model inference. It embeds the redacted prompt on-device, looks the embedding up in a persistent local ChromaDB vector store, and — when it finds a sufficiently confident and unambiguous match — serves the cached result, short-circuiting the expensive downstream work (compression + local inference). On a miss, the pipeline proceeds as normal and the produced result is stored so future semantically-equivalent prompts hit.

The cache is deliberately designed to resist the collision / cache-poisoning weakness of naive cosine-similarity caches. Rather than serving whatever single nearest neighbor clears a raw similarity threshold, the stage performs **cluster-based validation**: it retrieves the top-k nearest cached entries and serves a hit only when the top match both clears a minimum similarity *and* is separated from the runner-up by a clear confidence margin. If the top two candidates are too close, the match is ambiguous and the request is treated as a miss.

Everything runs entirely on-device, CPU-only, with no network call. The embedding model (`all-MiniLM-L6-v2` via sentence-transformers) is loaded from a local artifact at startup with graceful fallback, mirroring the redaction stage's NER-load pattern. Because caching happens strictly on the **redacted** prompt, no raw sensitive value ever enters the cache, its embeddings, or its metadata — reinforcing TokenQuick's provable-on-device-privacy differentiator. Every cache decision is written to an append-only local log (metadata and scores only, never the raw prompt) so the false-hit rate can be audited later.

The stage is independently toggleable and independently benchmarkable, consistent with the compression and redaction stages. The dashboard gains a cache panel driven by a new `cache_report` data annotation showing cache hit rate, estimated compute time saved, and tokens saved from cache hits specifically — separate from compression savings. A developer stress-test tool feeds adversarial near-duplicate prompts and reports how often naive single-threshold caching *would* have produced a wrong hit versus how often the hardened cluster-based approach does.

## Glossary

- **Semantic_Cache**: The backend pipeline stage that embeds the redacted prompt, performs a hardened lookup in the Vector_Store, serves a cached result on a hit, and stores the produced result on a miss. Runs after the Redaction_Stage and before compression and inference.
- **Redaction_Stage**: The existing backend stage that redacts sensitive data in the requirements summary and produces the Redacted_Prompt before compression. Owned by the pre-inference-redaction feature; referenced here only for ordering.
- **Redacted_Prompt**: The redacted requirements-summary text produced by the Redaction_Stage. This is the ONLY text the Semantic_Cache is permitted to embed, look up, or store.
- **Embedding_Model**: The local `all-MiniLM-L6-v2` sentence-transformers model that converts a Redacted_Prompt into an Embedding. Runs on-device, CPU-only, loaded from a local artifact. Never a cloud embedding API.
- **Embedding**: The fixed-length numeric vector produced by the Embedding_Model for a Redacted_Prompt.
- **Vector_Store**: The persistent local ChromaDB collection that stores Cache_Entries (Embedding + Cached_Result + Cache_Metadata) on-device. Never a cloud vector database.
- **Cache_Entry**: A single stored record in the Vector_Store consisting of an Embedding, the associated Cached_Result, and Cache_Metadata.
- **Cached_Result**: The generated response text (and its associated token/telemetry values) previously produced by the downstream pipeline for a Redacted_Prompt, stored so it can be served on a future hit.
- **Cache_Metadata**: The non-sensitive fields stored alongside a Cache_Entry (e.g. creation timestamp, task mode, stored token counts, stored inference time). Contains no raw sensitive value and no raw prompt text.
- **Similarity_Score**: The cosine similarity between the query Embedding and a candidate Cache_Entry Embedding, ranging from -1 to 1.
- **Top_K**: The configurable number of nearest Cache_Entries retrieved from the Vector_Store for a single lookup.
- **Top_Match**: The retrieved Cache_Entry with the highest Similarity_Score for a lookup.
- **Runner_Up**: The retrieved Cache_Entry with the second-highest Similarity_Score for a lookup.
- **Minimum_Similarity**: The configurable threshold the Top_Match's Similarity_Score must meet or exceed for a hit to be possible.
- **Confidence_Margin**: The difference between the Top_Match's Similarity_Score and the Runner_Up's Similarity_Score for a lookup.
- **Margin_Threshold**: The configurable minimum Confidence_Margin required for a hit; defends against collision / cache-poisoning by rejecting ambiguous matches.
- **Cache_Decision**: The outcome of a single lookup — either a hit (Cached_Result served) or a miss — together with the Similarity_Score of the Top_Match, the Similarity_Score of the Runner_Up, and the Confidence_Margin.
- **Cache_Hit**: A Cache_Decision in which the Top_Match meets Minimum_Similarity AND the Confidence_Margin meets or exceeds Margin_Threshold, causing the Cached_Result to be served and downstream work to be short-circuited.
- **Cache_Miss**: A Cache_Decision in which either the Minimum_Similarity condition or the Margin_Threshold condition is not met, causing the downstream pipeline to proceed.
- **Cache_Decision_Log**: An append-only local log file recording one entry per Cache_Decision with timestamp, session id, decision, scores, and margin, and never the raw prompt or any raw sensitive value.
- **Cache_Report**: The per-session and/or cumulative cache telemetry emitted to the dashboard via the `cache_report` data annotation (hit rate, estimated compute time saved, tokens saved from hits).
- **Cache_Benchmark**: The before/after measurement set for the Semantic_Cache (lookup latency, hit/miss, tokens and inference time saved on a hit), consistent with the compression and redaction benchmark patterns.
- **Cache_Stress_Test**: The developer tool that feeds an Adversarial_Prompt_Set through both naive single-threshold logic and the hardened cluster-based logic and reports comparative wrong-hit counts and rates.
- **Naive_Single_Threshold_Policy**: A baseline cache policy that serves the Top_Match whenever its Similarity_Score meets Minimum_Similarity, ignoring the Confidence_Margin. Used only by the Cache_Stress_Test for comparison against the hardened cluster-based policy.
- **Adversarial_Prompt_Set**: The developer-supplied collection of near-duplicate prompts (with known intended-match groupings) used as input to the Cache_Stress_Test.
- **Wrong_Hit**: A Cache_Decision that serves a Cached_Result belonging to a prompt whose intended-match group differs from the query prompt's group, as judged against the Adversarial_Prompt_Set's known groupings.
- **Backend**: The TokenQuick FastAPI application (`backend/main.py` and supporting modules).
- **Dashboard**: The TokenQuick React frontend right pane that renders live pipeline reports.
- **Session_Id**: The identifier for the current chat session (the `sessionId` sent on every generate request) used to scope Cache_Report and Cache_Decision_Log entries.

## Requirements

### Requirement 1: Local embedding of the redacted prompt

**User Story:** As a privacy-conscious user, I want prompts embedded locally by an on-device model, so that semantic lookup never sends prompt content to any cloud service.

#### Acceptance Criteria

1. WHEN the Semantic_Cache processes a Cache-enabled generate request, THE Semantic_Cache SHALL compute the Embedding of the Redacted_Prompt using the Embedding_Model.
2. THE Semantic_Cache SHALL embed only the Redacted_Prompt and SHALL NOT embed the raw requirements summary or any pre-redaction text.
3. THE Embedding_Model SHALL produce an Embedding whose dimensionality is fixed by the loaded model artifact and identical for every Embedding computed within a single Backend run.
4. THE Embedding_Model SHALL compute Embeddings using only a local on-device model artifact executed on CPU, SHALL NOT require a GPU, and THE Backend SHALL make no external network call for embedding.
5. WHEN the Backend starts, THE Backend SHALL load the Embedding_Model from a local model artifact before serving the first generate request.
6. IF the Embedding_Model artifact is unavailable or fails to load at startup, THEN THE Backend SHALL continue operating with the Semantic_Cache disabled, SHALL record that semantic caching is unavailable, and SHALL treat every subsequent generate request as a Cache_Miss without attempting an embedding or lookup.
7. WHERE the Embedding_Model fails to load, THE Backend SHALL cleanly disable the entire cache stage — embedding, lookup, and store — while continuing normal request processing.
8. WHERE the same Redacted_Prompt is embedded more than once by the same loaded Embedding_Model, THE Embedding_Model SHALL produce an identical Embedding vector each time.

### Requirement 2: Persistent local vector store

**User Story:** As a user, I want cached embeddings and results stored in a local database, so that semantically-equivalent prompts hit across restarts without any cloud storage.

#### Acceptance Criteria

1. WHEN the Backend starts, THE Backend SHALL open a persistent local ChromaDB collection on disk to serve as the Vector_Store before serving the first generate request.
2. THE Vector_Store SHALL store each Cache_Entry as an Embedding together with its Cached_Result and Cache_Metadata.
3. THE Vector_Store SHALL persist Cache_Entries across Backend restarts using local on-device storage only.
4. THE Vector_Store SHALL exclude the raw requirements summary, the raw prompt, and any raw sensitive value from every Embedding, Cached_Result, and Cache_Metadata field it stores.
5. THE Vector_Store SHALL store and query Cache_Entries using only local on-device operations, and THE Backend SHALL make no external network call for any Vector_Store read or write.
6. IF opening the Vector_Store fails at startup, THEN THE Backend SHALL continue operating with the Semantic_Cache disabled, SHALL record that the Vector_Store is unavailable, and SHALL treat every subsequent generate request as a Cache_Miss.

### Requirement 3: Hardened cluster-based lookup

**User Story:** As a user, I want cache hits validated by a confidence margin, so that colliding or poisoned near-duplicate entries do not cause a wrong result to be served.

#### Acceptance Criteria

1. WHEN the Semantic_Cache looks up a query Embedding, THE Semantic_Cache SHALL retrieve up to the Top_K nearest Cache_Entries from the Vector_Store ranked by descending Similarity_Score, where Similarity_Score is the cosine similarity (ranging from -1.0 to 1.0) between the query Embedding and each candidate Embedding.
2. WHEN the number of stored Cache_Entries is fewer than Top_K, THE Semantic_Cache SHALL retrieve all available Cache_Entries.
3. THE Semantic_Cache SHALL treat Top_K, Minimum_Similarity, and Margin_Threshold as configurable parameters with default values Top_K = 5, Minimum_Similarity = 0.85, and Margin_Threshold = 0.05, where Top_K is an integer >= 1, and Minimum_Similarity and Margin_Threshold are values in the range 0.0 to 1.0.
4. THE Semantic_Cache SHALL compute the Confidence_Margin as the Top_Match Similarity_Score minus the Runner_Up Similarity_Score.
5. WHEN the Top_Match Similarity_Score is greater than or equal to Minimum_Similarity AND the Confidence_Margin is greater than or equal to Margin_Threshold, THE Semantic_Cache SHALL record the Cache_Decision as a Cache_Hit and serve the Top_Match's Cached_Result.
6. IF the Top_Match Similarity_Score is less than Minimum_Similarity, THEN THE Semantic_Cache SHALL record the Cache_Decision as a Cache_Miss.
7. IF the Top_Match Similarity_Score is greater than or equal to Minimum_Similarity AND the Confidence_Margin is less than Margin_Threshold, THEN THE Semantic_Cache SHALL record the Cache_Decision as a Cache_Miss on the ground that the match is ambiguous.
8. WHEN two or more retrieved candidates share the highest Similarity_Score (a tie, including identical Embeddings), THE Semantic_Cache SHALL set the Confidence_Margin to 0.0 and, where Margin_Threshold is greater than 0.0, SHALL record the Cache_Decision as a Cache_Miss.
9. WHEN the Vector_Store returns exactly one candidate for a lookup, THE Semantic_Cache SHALL set the Runner_Up Similarity_Score to 0.0 and evaluate the Cache_Hit conditions using the resulting Confidence_Margin.
10. WHEN the Vector_Store returns zero candidates for a lookup, THE Semantic_Cache SHALL record the Cache_Decision as a Cache_Miss.

### Requirement 4: Append-only cache decision logging without raw values

**User Story:** As a security-conscious user, I want every cache decision logged with its scores, so that I can audit the false-hit rate later without the log exposing any prompt content.

#### Acceptance Criteria

1. WHEN the Semantic_Cache reaches a Cache_Decision for a generate request, THE Cache_Decision_Log SHALL append one entry containing an ISO-8601 timestamp, the Session_Id, the decision (hit or miss), the Top_Match Similarity_Score, the Runner_Up Similarity_Score, and the Confidence_Margin.
2. THE Cache_Decision_Log SHALL record entries to a local append-only file.
3. THE Cache_Decision_Log SHALL exclude the raw requirements summary, the raw prompt, the Redacted_Prompt text, and any raw sensitive value from every entry.
4. WHEN the Runner_Up does not exist for a lookup, THE Cache_Decision_Log SHALL record the Runner_Up Similarity_Score as a defined sentinel value indicating no runner-up was present.
5. WHEN a new entry is appended, THE Cache_Decision_Log SHALL preserve all previously written entries unchanged.
6. IF appending an entry to the Cache_Decision_Log fails, THEN THE Backend SHALL continue processing the generate request and record that the log entry could not be written, without exposing any raw prompt or sensitive value.

### Requirement 5: Cache stress-test developer tool

**User Story:** As a developer, I want a stress-test tool that compares naive and hardened caching on adversarial prompts, so that I can quantify how much the confidence margin reduces wrong hits.

#### Acceptance Criteria

1. THE Cache_Stress_Test SHALL be a local developer tool invoked outside the request-serving path and SHALL NOT be exposed as a network-served production endpoint.
2. THE Adversarial_Prompt_Set SHALL declare, for each prompt, an intended-match group label, and two prompts SHALL be considered the same intended-match group when their group labels are equal.
3. IF a prompt in the Adversarial_Prompt_Set has no intended-match group label, THEN THE Cache_Stress_Test SHALL reject the Adversarial_Prompt_Set and report that a group label was missing.
4. WHEN the Cache_Stress_Test is run with a non-empty Adversarial_Prompt_Set, THE Cache_Stress_Test SHALL evaluate each prompt under a naive single-threshold policy that serves the Top_Match whenever its Similarity_Score is greater than or equal to Minimum_Similarity, ignoring the Confidence_Margin.
5. WHEN the Cache_Stress_Test is run with a non-empty Adversarial_Prompt_Set, THE Cache_Stress_Test SHALL evaluate each prompt under the hardened cluster-based policy defined in Requirement 3.
6. THE Cache_Stress_Test SHALL count a Wrong_Hit whenever a policy serves a Cached_Result whose intended-match group label differs from the query prompt's intended-match group label.
7. WHEN the Cache_Stress_Test completes, THE Cache_Stress_Test SHALL report the total number of prompts evaluated, the naive Wrong_Hit count, the naive Wrong_Hit rate, the hardened Wrong_Hit count, and the hardened Wrong_Hit rate, where each Wrong_Hit rate is expressed as a value from 0.0 to 1.0.
8. WHEN the Cache_Stress_Test is run twice on the same Adversarial_Prompt_Set with the same configuration and the same loaded Embedding_Model, THE Cache_Stress_Test SHALL produce identical counts and rates.
9. THE Cache_Stress_Test SHALL embed the Adversarial_Prompt_Set using the Embedding_Model on-device, and THE Cache_Stress_Test SHALL make no external network call.
10. IF the Adversarial_Prompt_Set is empty, THEN THE Cache_Stress_Test SHALL report zero prompts evaluated and a Wrong_Hit rate of 0.0 for both policies.
11. THE Cache_Stress_Test SHALL exclude any raw sensitive value carried in the Adversarial_Prompt_Set from its report output.

### Requirement 6: Store the result on a cache miss

**User Story:** As a user, I want misses to populate the cache with successful results, so that future semantically-equivalent prompts hit and skip the expensive downstream work.

#### Acceptance Criteria

1. THE Semantic_Cache SHALL treat a Cached_Result as successful when the downstream pipeline completes and produces a non-empty result for the request, whether the result is a PERFORM output or a BUILD output.
2. WHEN a Cache_Miss occurs for a generate request AND the downstream pipeline produces a successful Cached_Result, THE Semantic_Cache SHALL store a new Cache_Entry in the Vector_Store containing the Redacted_Prompt Embedding, the Cached_Result, and the Cache_Metadata.
3. THE Semantic_Cache SHALL store in Cache_Metadata the token counts and inference time associated with producing the Cached_Result, using the real values reported by the local model, so later Cache_Hits can compute tokens saved and inference time saved.
4. THE Semantic_Cache SHALL store only the Redacted_Prompt Embedding and SHALL NOT store the raw requirements summary, the raw prompt, or any raw sensitive value in the Cache_Entry.
5. WHEN a Cache_Hit occurs for a generate request, THE Semantic_Cache SHALL serve the existing Cached_Result and SHALL NOT create a new Cache_Entry for that request.
6. IF the Redaction_Stage failed for a generate request, THEN THE Semantic_Cache SHALL NOT embed, look up, or store any Cache_Entry for that request.
7. IF the downstream pipeline does not produce a successful Cached_Result after a Cache_Miss, THEN THE Semantic_Cache SHALL leave the Vector_Store unchanged for that request.
8. IF storing a Cache_Entry in the Vector_Store fails, THEN THE Backend SHALL complete and return the produced result for the current request and record that the Cache_Entry could not be stored, without exposing any raw prompt or sensitive value.

### Requirement 7: Cache telemetry dashboard panel

**User Story:** As a user, I want a cache panel showing hit rate and savings, so that I can see the cache's benefit separately from compression savings.

#### Acceptance Criteria

1. WHEN the Semantic_Cache reaches a Cache_Decision for a generate request, THE Backend SHALL emit a `cache_report` data annotation tagged with the current Session_Id before the finish frame.
2. THE `cache_report` annotation SHALL report the cache hit rate for the current Session_Id computed as the number of Cache_Hits divided by the total number of Cache_Decisions in the session, expressed as a value from 0.0 to 1.0.
3. THE `cache_report` annotation SHALL report the cumulative estimated compute time saved from Cache_Hits in the session in milliseconds as a non-negative integer, and the cumulative tokens saved from Cache_Hits in the session as a non-negative integer.
4. THE `cache_report` annotation SHALL report tokens saved and compute time saved attributable to Cache_Hits in fields distinct from any compression-savings fields, so the Dashboard can present them separately.
5. THE `cache_report` annotation SHALL exclude the raw requirements summary, the raw prompt, and any raw sensitive value.
6. WHEN a Cache_Hit occurs, THE Backend SHALL derive the tokens saved and estimated inference time avoided for that request from the served Cached_Result's stored token and inference-time values in Cache_Metadata.
7. WHEN the Dashboard receives a `cache_report` annotation whose Session_Id matches the current session, THE Dashboard SHALL render the cache panel in the right pane showing the cache hit rate, the estimated compute time saved, and the tokens saved from Cache_Hits, visually separated from the compression-savings presentation.
8. IF the Dashboard receives a `cache_report` annotation whose Session_Id does not match the current session, THEN THE Dashboard SHALL leave the current cache panel contents unchanged.
9. WHILE the current session has zero Cache_Decisions, THE Dashboard SHALL display an empty-state message in the cache panel indicating that no cache activity has occurred in the current session.

### Requirement 8: Toggleable pipeline stage

**User Story:** As a user, I want to turn semantic caching on or off, so that I can control the pipeline behavior consistent with the other stages.

#### Acceptance Criteria

1. WHERE the Semantic_Cache is enabled, WHEN the generate phase produces the Redacted_Prompt, THE Backend SHALL perform an embedding and hardened lookup before invoking compression.
2. WHERE the Semantic_Cache is disabled, WHEN the generate phase produces the Redacted_Prompt, THE Backend SHALL pass the Redacted_Prompt to the compression step performing no embedding, no lookup, no store, and no Cache_Decision_Log append.
3. WHEN a client changes the Semantic_Cache toggle state through the Backend, THE Backend SHALL apply the new state to every generate request that begins after the change and SHALL leave any generate request already in progress running under the prior state.
4. WHEN a client requests the current Semantic_Cache toggle state, THE Backend SHALL return whether the stage is enabled or disabled.
5. WHERE the Semantic_Cache is disabled, WHEN the generate phase completes, THE Backend SHALL emit a `cache_report` annotation indicating that the stage was disabled for the request.
6. WHEN the Backend starts with no prior toggle state configured, THE Backend SHALL default the Semantic_Cache to enabled.

### Requirement 9: Pipeline placement and hit/miss ordering

**User Story:** As a privacy-conscious user, I want the cache to run only on redacted prompts and to short-circuit downstream work on a hit, so that no raw prompt is ever cached and hits skip compression and inference.

#### Acceptance Criteria

1. WHEN the generate phase runs with both stages enabled, THE Backend SHALL complete the Redaction_Stage and produce the Redacted_Prompt before the Semantic_Cache performs any embedding or lookup.
2. THE Semantic_Cache SHALL use the Redacted_Prompt, and only the Redacted_Prompt, as the input to embedding and lookup.
3. WHEN a Cache_Hit occurs, THE Backend SHALL serve the Cached_Result and SHALL invoke neither the compression step, nor the PERFORM inference path, nor the BUILD task-router path for that request.
4. WHEN a Cache_Hit occurs, THE Backend SHALL stream the served Cached_Result to the client using the same data-stream frame types and ordering as a freshly produced result, ending with the finish frame.
5. WHEN a Cache_Hit occurs, THE Backend SHALL emit the `cache_report` annotation marked as a hit together with the redaction annotations before the finish frame.
6. WHEN a Cache_Hit occurs, THE Backend SHALL emit the compression-related annotations with values that explicitly indicate compression was skipped for the request, so a consumer can distinguish a hit-skipped compression from an executed one.
7. WHEN a Cache_Miss occurs, THE Backend SHALL proceed to the compression step and the applicable PERFORM or BUILD path unchanged, and then apply Requirement 6 to store the produced result.
8. THE Semantic_Cache SHALL NOT embed, look up, or store the raw requirements summary or any pre-redaction text under any condition.
9. WHEN a generate request is received without a Session_Id, THE Backend SHALL reject the request before any embedding or lookup is performed, consistent with the existing generate-phase Session_Id requirement.

### Requirement 10: Cache benchmarking (before/after)

**User Story:** As a user, I want before/after benchmark numbers for the cache, so that I can measure the stage's cost and savings consistent with how compression and redaction are benchmarked.

#### Acceptance Criteria

1. WHEN the Semantic_Cache reaches a Cache_Decision, THE Cache_Benchmark SHALL capture the lookup latency for the stage as a non-negative value in milliseconds, measured from lookup start to Cache_Decision.
2. WHEN the Semantic_Cache reaches a Cache_Decision, THE Cache_Benchmark SHALL capture whether the decision was a Cache_Hit or a Cache_Miss.
3. WHEN a Cache_Hit occurs, THE Cache_Benchmark SHALL capture the tokens saved and the estimated inference time saved for the request as non-negative values derived from the Cached_Result's stored values.
4. WHEN a Cache_Miss occurs, THE Cache_Benchmark SHALL report tokens saved and estimated inference time saved as zero for the request.
5. WHEN the Semantic_Cache reaches a Cache_Decision, THE Backend SHALL include the lookup latency, the hit/miss outcome, and the tokens and inference time saved from the Cache_Benchmark in a data annotation emitted for the Semantic_Cache before the finish frame.
6. WHEN the Dashboard receives the Semantic_Cache data annotation, THE Dashboard SHALL display the lookup latency, hit/miss outcome, and tokens and inference time saved in the right pane using the same layout, labeling, and units as the compression and redaction benchmark presentations.
7. IF the Cache_Benchmark fails to capture any benchmark value, THEN THE Backend SHALL emit a data annotation indicating the cache benchmark is unavailable and SHALL continue processing without blocking the Semantic_Cache output.

## Non-Goals and Constraints

- **Nothing leaves the machine.** THE Semantic_Cache, THE Embedding_Model, THE Vector_Store, THE Cache_Decision_Log, and THE Cache_Stress_Test SHALL operate entirely on-device, CPU-only, and THE Backend SHALL make no external network call for embedding, lookup, storage, logging, benchmarking, or stress testing.
- **Redacted prompts only.** The Semantic_Cache operates strictly on the Redacted_Prompt produced by the Redaction_Stage; it never embeds, looks up, or stores the raw prompt, the raw requirements summary, or any raw sensitive value. Because caching happens post-redaction, Cache_Entries and Embeddings never contain raw secrets.
- **Single-machine, single-user scope.** This feature is a single-machine, single-user or small-business tool. It does not add a distributed, multi-tenant, or cross-machine shared cache.
- **No cloud services.** Cloud vector databases (e.g. hosted Pinecone-style services) and cloud embedding APIs are explicitly out of scope. The embedding model is `all-MiniLM-L6-v2` via sentence-transformers and the vector store is a local ChromaDB collection.
- The Semantic_Cache applies to the requirements summary within the generate phase; it does not alter the elicitation (scoping) conversation flow.
