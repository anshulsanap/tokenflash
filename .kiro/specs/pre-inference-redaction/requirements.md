# Requirements Document

## Introduction

TokenQuick adds a **pre-inference redaction** stage to its FastAPI backend. Before any prompt reaches the existing heuristic compression step in the `/api/chat` "generate" phase, the stage scans the requirements summary for sensitive data and redacts it in place, so the redacted text is what flows into `compress_prompt_detailed` and, subsequently, the local model.

Detection runs entirely on-device: local regular expressions for structured secrets (SSNs, credit card numbers, emails, phone numbers, API keys/tokens) plus a lightweight local NER model for names and entities, and a user-configurable list of custom terms loaded from a local config file. Every redaction is recorded to an append-only local audit log capturing only the category, timestamp, and session id — never the raw value. The dashboard gains a "Redaction Report" panel driven by a new `redaction_report` data annotation, mirroring how `compression_stats` drives the compression report. The stage is independently toggleable and independently benchmarkable (before/after numbers), consistent with the compression stage. A settings screen lets the user add and remove custom terms without restarting the backend.

This feature reinforces TokenQuick's core differentiator — provable on-device privacy. No prompt content, sensitive value, or telemetry ever leaves the machine.

## Glossary

- **Redaction_Stage**: The backend pipeline stage that scans and redacts sensitive data in the requirements summary before compression.
- **Detector**: A component that identifies spans of one sensitive Category within input text. Detectors include the regex Detectors, the NER Detector, and the Custom_Term Detector.
- **Category**: A named class of sensitive data. Built-in categories are `ssn`, `credit_card`, `email`, `phone`, `api_key`, and `person` (NER-detected names/entities). Custom terms are recorded under the `custom_term` category.
- **NER_Model**: A lightweight local named-entity-recognition model (e.g. a spaCy small model) that runs on-device and identifies person/entity spans. Never a cloud NER service.
- **Custom_Terms_Config**: A local configuration file listing user-defined redaction terms (e.g. client names, internal project codenames).
- **Redaction_Placeholder**: The category-labelled token that replaces a detected sensitive span in the redacted text (e.g. `[REDACTED:email]`).
- **Audit_Log**: An append-only local log file recording one entry per redaction with timestamp, category, and session id, and never the raw sensitive value.
- **Redaction_Report**: The per-session summary of redactions grouped and counted by Category, emitted to the dashboard via the `redaction_report` data annotation.
- **Redaction_Benchmark**: The before/after measurement set for the Redaction_Stage (redaction latency, count of items redacted by category, characters redacted), consistent with the compression stats pattern.
- **Settings_Screen**: The frontend surface where the user views, adds, and removes custom redaction terms.
- **Backend**: The TokenQuick FastAPI application (`backend/main.py` and supporting modules).
- **Dashboard**: The TokenQuick React frontend right pane that renders live pipeline reports.
- **Session_Id**: The identifier for the current chat session used to scope the Redaction_Report and Audit_Log entries.
- **Redacted_Summary**: The output of the Redaction_Stage after redaction.
- **Sensitive_Span**: A contiguous run of characters classified into a defined sensitive Category.

## Requirements

### Requirement 1: Redaction stage runs before compression

**User Story:** As a privacy-conscious user, I want sensitive data redacted before compression, so that no secret ever reaches the compression step, the local model, or any downstream artifact.

#### Acceptance Criteria

1. WHEN the `/api/chat` generate phase produces the requirements summary, THE Redaction_Stage SHALL complete redaction of that summary and produce a Redacted_Summary before `compress_prompt_detailed` is invoked.
2. THE Redaction_Stage SHALL pass the Redacted_Summary, and only the Redacted_Summary, as the text input to the compression step.
3. WHERE a Sensitive_Span (a contiguous run of characters classified into a defined sensitive Category) is detected, THE Redaction_Stage SHALL replace the entire span with a Redaction_Placeholder consisting of a fixed delimiter and the span's Category label, such that the placeholder contains no character from the original span.
4. THE Redaction_Stage SHALL preserve every character of the surrounding non-sensitive text byte-for-byte, altering only the detected Sensitive_Spans.
5. THE Redaction_Stage SHALL ensure that no substring of any detected Sensitive_Span (minimum length 4 characters) appears in the compressor input, in any emitted generate-phase data annotation (including the compression statistics and before/after compression diff), in the local model input, or in the streamed output.
6. IF the summary contains no detected Sensitive_Span, THEN THE Redaction_Stage SHALL return the summary unchanged and pass it to the compression step.
7. IF redaction cannot complete (a redaction failure occurs), THEN THE Redaction_Stage SHALL block invocation of `compress_prompt_detailed`, suppress the unredacted summary from all downstream steps and annotations, and emit a data annotation indicating redaction failure.

### Requirement 2: Detect and redact built-in structured secrets

**User Story:** As a user, I want structured secrets detected by local pattern matching, so that SSNs, cards, contact details, and API keys are removed reliably.

#### Acceptance Criteria

1. WHEN input text contains a value matching the Social Security Number pattern, THE Detector SHALL replace each matched value with a redaction placeholder labeled with Category `ssn`, such that the original value does not appear in the output text.
2. WHEN input text contains a value matching the credit card number pattern, THE Detector SHALL replace each matched value with a redaction placeholder labeled with Category `credit_card`, such that the original value does not appear in the output text.
3. WHEN input text contains a value matching the email address pattern, THE Detector SHALL replace each matched value with a redaction placeholder labeled with Category `email`, such that the original value does not appear in the output text.
4. WHEN input text contains a value matching the phone number pattern, THE Detector SHALL replace each matched value with a redaction placeholder labeled with Category `phone`, such that the original value does not appear in the output text.
5. WHEN input text contains a value matching an API key or token pattern, including `sk-` prefixed keys, `AKIA` prefixed keys, `ghp_` prefixed tokens, and `Bearer` tokens, THE Detector SHALL replace each matched value with a redaction placeholder labeled with Category `api_key`, such that the original value does not appear in the output text.
6. THE Detector SHALL identify built-in structured secrets using only local regular expressions executed on-device, without making any network request.
7. WHEN input text contains more than one value matching any built-in pattern, THE Detector SHALL redact every matched value independently in a single pass.
8. IF a single value matches more than one built-in category pattern, THEN THE Detector SHALL redact the value once under the highest-priority category using the fixed precedence order `ssn`, `credit_card`, `api_key`, `email`, `phone`.
9. WHEN input text contains no value matching any built-in pattern, THE Detector SHALL return the input text unchanged.

### Requirement 3: Local NER-based name and entity detection

**User Story:** As a user, I want names and entities detected by a local model, so that identities are redacted without any cloud service.

#### Acceptance Criteria

1. WHEN input text contains a person or entity span identified by the NER_Model and that span does not overlap a span already detected by a regex Detector or the Custom_Term Detector, THE Detector SHALL redact the span under Category `person`.
2. IF an NER_Model span overlaps a span already detected by a regex Detector or the Custom_Term Detector, THEN THE Detector SHALL redact the overlapping region once under the Category of the earlier-matched Detector and SHALL NOT emit a duplicate `person` redaction for the same characters.
3. THE NER_Model SHALL identify person/entity spans using only a local on-device model artifact, and THE Backend SHALL make no external network call for NER detection.
4. WHEN the Backend starts, THE Backend SHALL load the NER_Model from a local model artifact before serving the first generate request.
5. IF the NER_Model artifact is unavailable or fails to load at startup, THEN THE Backend SHALL continue operating the regex Detectors and the Custom_Term Detector, SHALL record that NER detection is disabled, and SHALL NOT emit any `person` redaction until the NER_Model is loaded.

### Requirement 4: User-configurable custom terms with live reload

**User Story:** As a user, I want to maintain a list of custom redaction terms, so that client names and internal codenames are redacted without restarting the backend.

#### Acceptance Criteria

1. WHEN the Backend starts, THE Backend SHALL load custom redaction terms from the local Custom_Terms_Config, accepting up to 10,000 terms where each term is between 1 and 256 characters in length.
2. WHEN input text contains a term listed in the Custom_Terms_Config, THE Custom_Term Detector SHALL redact each matching span under Category `custom_term`, where a match is a case-insensitive occurrence of the full term.
3. IF a term in the Custom_Terms_Config is empty, exceeds 256 characters, or is a duplicate of an already-loaded term, THEN THE Backend SHALL skip that term, load the remaining valid terms, and record an entry indicating the term was skipped and the reason.
4. WHEN a custom term is added through the Backend, THE Backend SHALL apply the added term to all redaction requests received more than 2 seconds after the addition, without a restart.
5. WHEN a custom term is removed through the Backend, THE Backend SHALL stop redacting that term on all redaction requests received more than 2 seconds after the removal, without a restart.
6. WHEN the Custom_Terms_Config file is modified on disk, THE Backend SHALL reload the custom-term list and apply the updated list to all redaction requests received more than 2 seconds after the modification, without a restart.
7. WHEN a custom term is added or removed through the Backend, THE Backend SHALL persist the resulting custom-term list to the Custom_Terms_Config.
8. IF persisting the custom-term list to the Custom_Terms_Config fails, THEN THE Backend SHALL retain the in-memory custom-term list, leave the prior Custom_Terms_Config contents unchanged, and return a response indicating the persistence failure.
9. IF the Custom_Terms_Config is missing or unreadable when the Backend starts, THEN THE Backend SHALL start with an empty custom-term list and record an entry indicating that the configuration was not loaded.

### Requirement 5: Custom-terms settings endpoints

**User Story:** As a user, I want backend endpoints to manage custom terms, so that the settings screen can read and update the term list at runtime.

#### Acceptance Criteria

1. WHEN a client requests the current custom terms, THE Backend SHALL return the list of custom terms from the active configuration, returning an empty list when no terms are configured.
2. WHEN a client submits a new custom term, THE Backend SHALL trim leading and trailing whitespace, add the normalized term to the active configuration, and return the updated list.
3. WHEN a client submits a custom term that already exists under case-insensitive comparison, THE Backend SHALL leave the configuration unchanged and return the existing list.
4. WHEN a client requests removal of an existing custom term matched case-insensitively, THE Backend SHALL remove the term from the active configuration and return the updated list.
5. IF a client submits a custom term that is empty after trimming or exceeds 256 characters, THEN THE Backend SHALL reject the request, leave the configuration unchanged, and return a descriptive error.
6. IF a client requests removal of a term that is not present, THEN THE Backend SHALL leave the configuration unchanged and return a status indicating the term was absent.
7. IF persisting a custom-term change fails, THEN THE Backend SHALL return a descriptive error indicating the change was not persisted.

### Requirement 6: Append-only audit logging without raw values

**User Story:** As a security-conscious user, I want an audit trail of what was redacted, so that I can verify redaction happened without the log itself exposing any secret.

#### Acceptance Criteria

1. WHEN a sensitive span is redacted, THE Audit_Log SHALL append one entry containing an ISO-8601 timestamp, the Category, and the Session_Id.
2. THE Audit_Log SHALL record entries to a local append-only file.
3. THE Audit_Log SHALL exclude the raw sensitive value, and any substring of it of length 4 or greater, from every entry.
4. WHEN a new entry is appended, THE Audit_Log SHALL preserve all previously written entries unchanged.
5. IF appending an entry to the Audit_Log fails, THEN THE Backend SHALL continue processing the redaction and record that the audit entry could not be written, without exposing the raw sensitive value.

### Requirement 7: Redaction Report dashboard panel

**User Story:** As a user, I want a Redaction Report panel in the dashboard, so that I can see what was caught in the current session, counted by category.

#### Acceptance Criteria

1. WHEN the Redaction_Stage completes for a generate request, THE Backend SHALL emit a `redaction_report` data annotation tagged with the current Session_Id.
2. THE `redaction_report` annotation SHALL include, for the current Session_Id, one count entry per Category that has at least one redaction, where each count is the cumulative number of redactions recorded for that Category across all generate requests in the session.
3. THE `redaction_report` annotation SHALL exclude the raw sensitive values.
4. WHEN the Dashboard receives a `redaction_report` annotation whose Session_Id matches the current session, THE Dashboard SHALL render the Redaction_Report panel in the right pane listing each reported Category with its redaction count.
5. IF the Dashboard receives a `redaction_report` annotation whose Session_Id does not match the current session, THEN THE Dashboard SHALL leave the current Redaction_Report panel contents unchanged.
6. WHILE the current session has zero redactions across all Categories, THE Dashboard SHALL display an empty-state message in the Redaction_Report panel indicating that no sensitive data was detected in the current session.

### Requirement 8: Toggleable pipeline stage

**User Story:** As a user, I want to turn redaction on or off, so that I can control the pipeline behavior consistent with other stages.

#### Acceptance Criteria

1. WHERE the Redaction_Stage is enabled, WHEN the generate phase produces the requirements summary, THE Backend SHALL scan and redact the requirements summary before `compress_prompt_detailed` is called.
2. WHERE the Redaction_Stage is disabled, WHEN the generate phase produces the requirements summary, THE Backend SHALL pass the requirements summary to the compression step byte-for-byte unchanged, performing no scan, no redaction, and no Audit_Log append.
3. WHEN a client changes the Redaction_Stage toggle state through the Backend, THE Backend SHALL apply the new state to every generate request that begins after the change and SHALL leave any generate request already in progress running under the prior state.
4. WHEN a client requests the current Redaction_Stage toggle state, THE Backend SHALL return whether the stage is enabled or disabled.
5. WHERE the Redaction_Stage is disabled, WHEN the generate phase completes, THE Backend SHALL emit a `redaction_report` annotation indicating zero redactions and that the stage was disabled for the request.
6. WHEN the Backend starts with no prior toggle state configured, THE Backend SHALL default the Redaction_Stage to enabled.

### Requirement 9: Redaction benchmarking (before/after)

**User Story:** As a user, I want before/after benchmark numbers for redaction, so that I can measure the stage's cost consistent with how compression is benchmarked.

#### Acceptance Criteria

1. WHEN the Redaction_Stage finishes processing a requirements summary, THE Redaction_Benchmark SHALL capture the redaction latency for the stage as a non-negative value in milliseconds, measured from stage start to stage completion.
2. WHEN the Redaction_Stage finishes processing a requirements summary, THE Redaction_Benchmark SHALL capture the count of items redacted per Category as a non-negative integer for each Category, defaulting to 0 for any Category with no redactions.
3. WHEN the Redaction_Stage finishes processing a requirements summary, THE Redaction_Benchmark SHALL capture the total number of characters redacted as a non-negative integer.
4. WHEN the Redaction_Stage finishes processing a requirements summary, THE Backend SHALL include the redaction latency, per-Category redacted counts, and characters redacted from the Redaction_Benchmark in the data annotation emitted for the Redaction_Stage before the finish frame.
5. WHEN the Dashboard receives the Redaction_Stage data annotation, THE Dashboard SHALL display the redaction latency, per-Category redacted counts, and characters redacted in the right pane using the same layout, labeling, and units as the compression stats presentation.
6. IF the Redaction_Stage redacts zero items, THEN THE Redaction_Benchmark SHALL report a redacted-item count of 0 for every Category and 0 characters redacted, and THE Dashboard SHALL display these zero values in the right pane.
7. IF the Redaction_Benchmark fails to capture any benchmark value, THEN THE Backend SHALL emit a data annotation indicating the redaction benchmark is unavailable and SHALL continue processing without blocking the Redaction_Stage output.

### Requirement 10: Custom-terms settings screen

**User Story:** As a user, I want a settings screen for custom terms, so that I can add and remove terms through the interface without restarting the backend.

#### Acceptance Criteria

1. WHEN the user opens the Settings_Screen, THE Dashboard SHALL request the current custom terms from the Backend and display the list returned by the Backend.
2. WHEN the user adds a custom term through the Settings_Screen, THE Dashboard SHALL submit the term to the Backend and, on a successful response, display the updated list returned by the Backend.
3. WHEN the user removes a custom term through the Settings_Screen, THE Dashboard SHALL submit the removal to the Backend and, on a successful response, display the updated list returned by the Backend.
4. IF the Backend rejects or does not complete a custom-term add or removal, THEN THE Settings_Screen SHALL display an error indicating the change was not applied and SHALL retain the list that was displayed before the change.
5. IF the request for the current custom terms fails when the Settings_Screen is opened, THEN THE Settings_Screen SHALL display an error indicating the terms could not be loaded and SHALL NOT display a partial or empty list as the current terms.
6. WHILE an add or removal request submitted from the Settings_Screen is in progress, THE Settings_Screen SHALL indicate that the change is pending and SHALL NOT report the change as applied until the Backend response is received.

### Requirement 11: Placeholder preservation through compression

**User Story:** As a privacy-conscious user, I want every Redaction_Placeholder to survive the compression step intact, so that a redacted secret's category label is never corrupted and no placeholder is lost before the redacted text reaches the local model.

#### Acceptance Criteria

1. WHEN the compression step processes the Redacted_Summary, THE Backend SHALL ensure that every Redaction_Placeholder present in the Redacted_Summary appears in the compressor output intact and unaltered.
2. WHILE the compression step scores tokens for preservation or discard, THE Backend SHALL retain every Redaction_Placeholder regardless of its heuristic token score, and SHALL NOT discard, split, or merge a Redaction_Placeholder with any adjacent token.
3. THE Backend SHALL produce a compressor output whose count of Redaction_Placeholders and whose set of placeholder Category labels are identical to the count and Category labels present in the Redacted_Summary.
4. THE Backend SHALL preserve the characters of each Redaction_Placeholder byte-for-byte through the compression step, including the fixed delimiter, the bracket characters, and the Category label.
5. IF the compression step detects that a Redaction_Placeholder cannot be preserved or has been corrupted, THEN THE Backend SHALL treat the outcome as a redaction failure and apply Requirement 1.7, rather than emitting the corrupted compressor output to any downstream step or annotation.

## Non-Goals and Constraints

- **Nothing leaves the machine.** THE Redaction_Stage, THE NER_Model, THE Audit_Log, and all custom-term storage SHALL operate entirely on-device, and THE Backend SHALL make no external network call for detection, redaction, logging, or benchmarking.
- Redaction is applied to the requirements summary within the generate phase; it does not alter the elicitation (scoping) conversation flow.
- This feature does not add multi-tenant, fleet-scale, or cross-machine governance; it remains a single-machine, single-user or small-business tool.
- Cloud-based NER, cloud secret scanners, and cloud log aggregation are explicitly out of scope.
