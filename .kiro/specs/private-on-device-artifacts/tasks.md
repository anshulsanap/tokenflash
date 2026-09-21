# Implementation Plan: Private On-Device Artifacts (Sandpack Live Preview Pane)

## Overview

This plan converts the private-on-device-artifacts design into incremental,
bottom-up coding steps that mirror the existing pipeline stages (redaction /
cache / power telemetry): a backend state singleton + toggle endpoints, a pure
frontend parser, a presentational preview component with a network-restricted
Sandpack sandbox, and page.tsx / settings wiring. Each step ends by wiring new
code into the running system so nothing is orphaned.

**Test policy (per explicit user directive — lean, UI/integration, NOT
privacy-critical):** the MANDATORY automated coverage is lightweight **unit tests
on the `ArtifactStreamParser` state transitions** (Task 3) plus the **focused
security check** for the egress restriction + honest verification, including the
auto-submitting-form vector (Task 8). These are normal required checkboxes.
Property-based testing is explicitly NOT used for this stage. Any heavier or
nice-to-have tests are marked OPTIONAL/deferrable with the `- [ ]*` convention.

## Tasks

- [x] 1. Add the artifact stage state singleton (`backend/artifact_state.py`)
  - Create `backend/artifact_state.py` mirroring `power_state.py`: a thread-safe
    `ArtifactState` (`threading.Lock`) with `is_enabled()` / `set_enabled(value)`,
    master toggle default **enabled** (design §1, resolves Q6). Expose a
    module-level singleton `state = ArtifactState()`. In-memory only,
    JSON-serializable.
  - _Requirements: 8.1, 8.4_

- [x] 2. Inject the delimiter instruction on the BUILD path + add toggle endpoints (`backend/main.py`)
  - [x] 2.1 Import `artifact_state`; in the `generate` branch, AFTER intent
    routing, WHEN `artifact_state.is_enabled()` AND the request routed to BUILD,
    append the Artifact_Delimiter instruction to the generation prompt instructing
    the model to wrap ONE renderable artifact in `<<<TOKENQUICK_ARTIFACT:lang>>>` …
    `<<<END_ARTIFACT>>>` with `lang` in the supported set. NEVER inject on the
    PERFORM path. Do not disturb redaction/cache/power wiring.
    - _Requirements: 1.1, 6.1, 6.2, 6.3_
  - [x] 2.2 Add `GET /api/artifacts/toggle` → `{"enabled": ...}` and
    `POST /api/artifacts/toggle` (reuse `ToggleBody`) → `set_enabled(...)`,
    returning the new state. Mirror the `/api/power/toggle` pattern exactly; new
    state applies to generations that BEGIN after the change (in-flight keep their
    snapshot). Toggling never alters other stages.
    - _Requirements: 8.1, 8.3, 8.4_

- [x] 3. Implement the streaming parser (`frontend/lib/artifactParser.ts`)
  - Implement `ArtifactStreamParser` as a pure, synchronous, side-effect-free
    state machine (states `OUTSIDE | IN_ARTIFACT | CLOSED`) with `push(chunk)`,
    `snapshot()`, and `onStreamEnd()`. Export `OPEN_PREFIX`, `OPEN_SUFFIX`,
    `CLOSE_MARKER`, `ParserState`, and the `ParseResult` interface (design §2).
  - Straddle buffering: retain a `pending` tail that is a prefix of any marker
    until completed, so a marker split across chunks never yields a false
    open/close (Req 2.3). While `IN_ARTIFACT`, ONLY the exact `CLOSE_MARKER` ends
    the block; backticks/fences are literal content (Req 1.3). Parse `lang` from
    the open marker; empty/unknown → `null` (Req 1.4). Track `sawAnyMarker`
    (Req 1.5). First opened block is the deterministic Primary_Artifact; later
    complete blocks go to `extraBlocks`, never to Sandpack (Req 9.1, 9.3, 9.4).
    Never throw; always return a coherent snapshot (Req 2.4).
  - _Requirements: 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 9.1, 9.3, 9.4_

- [x] 4. Write the MANDATORY parser unit tests (`frontend/lib/artifactParser.test.ts`) — REQUIRED, not deferrable
  - **MANDATORY — the load-bearing automated coverage for this stage.** Lightweight
    unit tests (no property-based testing) over fixed chunk sequences:
    1. `OUTSIDE → IN_ARTIFACT → CLOSED` happy path, single block.
    2. Split-marker buffering: open AND close markers split across two `push`
       calls at every byte offset; assert no false open/close and correct final
       code (Req 2.3).
    3. Backticks/nested fences and a near-miss `<<<END` string inside the body;
       block only closes on the exact `CLOSE_MARKER` (Req 1.3).
    4. No-artifact stream → `sawAnyMarker === false`, empty `primaryCode` (Req 1.5).
    5. Stream-end-while-open: `onStreamEnd()` with a partial body → still
       `IN_ARTIFACT` (component would fall back) (Req 7.4).
    6. Multiple blocks: first is `primaryCode`, second in `extraBlocks`;
       deterministic primary selection (Req 9.4).
    7. Language-tag parse: `html`, `react`, empty, unknown (Req 1.4).
  - _Requirements: 1.3, 1.4, 1.5, 2.3, 2.4, 2.5, 7.4, 9.4_

- [x] 5. Checkpoint - parser + backend toggle complete
  - Ensure the parser unit tests pass and the backend imports/routes register;
    ask the user if questions arise.

- [x] 6. Build the network-restricted Sandpack preview (`frontend/components/ArtifactPreview.tsx`)
  - [x] 6.1 Static preview + egress-restricted sandbox (PREVENTION, Req 4)
    - Render the CLOSED primary artifact via the Sandpack **STATIC** client (design
      §7): `html` (default when tag missing/unknown) uses vanilla HTML/CSS/JS; the
      `react` template uses a pre-vendored/pre-bundled static React bundle served
      from our own origin (no runtime package fetch). Do NOT use the default remote
      runtime bundler.
    - The preview iframe uses `sandbox` with `allow-scripts` but WITHOUT
      `allow-same-origin` (opaque origin). Inject the preview CSP with ALL of:
      `default-src 'none'; connect-src 'none'; form-action 'none'; frame-ancestors
      'none'; base-uri 'none';` plus only the minimal `script-src`/`style-src`/
      `img-src` the static template needs. The three non-inheriting directives
      (`form-action`, `frame-ancestors`, `base-uri`) MUST be explicit so the
      auto-submitting-form navigation vector is closed. Applied to every preview
      regardless of content; no third-party allowlist.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, 4.4, 4.5_
  - [x] 6.2 In-sandbox egress instrumentation + honest indicator (VERIFICATION, Req 5)
    - Inject into the static preview bootstrap (inside the sandbox): a
      `securitypolicyviolation` listener (matching `connect-src` AND
      `form-action`/`base-uri` violated directives) plus capture-phase wrappers
      that intercept and count blocked `fetch`/`XMLHttpRequest`/`WebSocket`/
      `EventSource`/`sendBeacon` attempts AND a capture-phase `submit` listener that
      `preventDefault()`s form submissions and counts them. Report the tally to the
      host via `postMessage`.
    - Render `EgressIndicator` from the derived `EgressStatus`: "0 outbound calls
      (verified)" when the handshake is live and count is 0; "blocked N outbound
      attempt(s)" when ≥1; "restriction configured (not runtime-verified)" when the
      handshake did not establish. NEVER derive "verified" from CSP config presence.
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_
  - [x] 6.3 Fallback + timeout classification (Req 7)
    - Compile/bundle error on a CLOSED block → raw-code fallback + brief error
      affordance (Req 7.1). Stream-end-while-open → raw-code fallback (Req 7.4).
      Idle stall: track time since the last token that changed `primaryCode`; fall
      back only after `INCOMPLETE_BLOCK_IDLE_TIMEOUT_MS = 4000` of no new tokens
      while `IN_ARTIFACT` (idle/stall-based, NOT wall-clock-since-open) so steady
      slow streaming never trips it (Req 7.2, 7.3, 7.5). All fallbacks show the raw
      artifact text verbatim, no partial execution (Req 7.6).
    - Additional (`extraBlocks`) blocks render as plain, NON-executed code beneath
      the preview (Req 9.2b, 9.3). Redaction placeholders render verbatim as literal
      mock text — no un-redaction, no redaction-state coupling (Req 10.1, 10.2, 10.3).
    - _Requirements: 6.5 (n/a), 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 9.2, 9.3, 10.1, 10.2, 10.3_

- [x] 7. Wire the stage into `frontend/app/page.tsx` and the settings panel
  - [x] 7.1 Instantiate one `ArtifactStreamParser` per generation; feed each `0:`
    text delta into `parser.push(delta)` in the existing stream loop; call
    `parser.onStreamEnd()` on the `d:` finish frame / stream close. Hold
    `artifactState` React state from the parser snapshot; render `<ArtifactPreview>`
    in the right pane. Reset to `null` in both `triggerGenerate` and `resetChat`.
    - _Requirements: 2.2, 6.3, 7.4_
  - [x] 7.2 Add a fourth "Live Preview" toggle row to `RedactionSettings.tsx`
    beside redaction / cache / power, reusing the switch markup; add
    `artifactEnabled` / pending / error state, load it in the initial `Promise.all`
    (add `fetch(\`${BACKEND}/api/artifacts/toggle\`)`), and flip it via
    `handleArtifactToggle` mirroring `handlePowerToggle` against
    `/api/artifacts/toggle`. WHEN off, no preview renders and the generate flow is
    unchanged.
    - _Requirements: 8.1, 8.2, 8.3, 8.4_

- [x] 8. Write the MANDATORY focused security check (egress prevention + honest verification) — REQUIRED, not deferrable
  - **MANDATORY — the load-bearing security coverage for this stage** (not PBT).
    A focused integration/DOM check:
    1. Assert the preview iframe has `allow-scripts` WITHOUT `allow-same-origin`
       and that the injected CSP contains ALL of `default-src 'none'`,
       `connect-src 'none'`, `form-action 'none'`, `frame-ancestors 'none'`, and
       `base-uri 'none'`.
    2. A test artifact that calls `fetch()` on run → assert it is blocked AND the
       egress indicator reports a blocked attempt; a clean artifact → "verified-zero".
    3. A test artifact with a **hidden auto-submitting `<form>` targeting an
       external URL** (script-triggered `.submit()` on load) → assert the submission
       is BOTH blocked (no off-origin navigation/request leaves the sandbox) AND
       reported by the egress indicator as a blocked outbound attempt, via the SAME
       path as the `fetch()` case.
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 5.1, 5.2, 5.3, 5.4, 5.5_

- [ ]* 9. Optional: backend toggle/injection unit test
  - OPTIONAL/deferrable. Assert `/api/artifacts/toggle` round-trips and the
    delimiter instruction is injected ONLY on the BUILD path with the stage
    enabled (never on PERFORM, never when disabled).
  - _Requirements: 6.1, 8.1, 8.2_

- [ ]* 10. Optional: ArtifactPreview component render tests
  - OPTIONAL/deferrable. Render the CLOSED/ok, bundle-error fallback,
    stream-end-open fallback, idle-timeout fallback, extra-blocks-as-plain-code,
    and redaction-placeholder-verbatim states; assert each affordance.
  - _Requirements: 6.1, 7.1, 7.4, 7.6, 9.2, 10.2_

- [x] 11. Final checkpoint - ensure all tests pass
  - Ensure the mandatory parser unit tests and the focused security check pass, the
    frontend build is clean, and the backend suite is green; ask the user if
    questions arise.

- [ ] 12. Implement the vendored/pre-bundled static React preview (design §7)
  - **Tracked follow-up (deliberate scope decision, not drift).** Phase 4 ships
    `html`-only live preview; the `react` template currently shows the raw-code
    fallback with an explicit "Live preview not yet available for React" note.
  - Implement a live React preview using a PRE-VENDORED / PRE-BUNDLED React +
    ReactDOM bundle served from OUR OWN origin (or inlined into the srcdoc), so
    the `react` template compiles/runs in the same opaque-origin, CSP-restricted
    sandbox as `html` with NO runtime npm fetch and NO network bundler
    (design §7 option (a); must not violate Req 4's deny-outbound/no-allowlist
    posture). Reuse the SAME PREVIEW_CSP + PREVIEW_SANDBOX + in-sandbox egress
    bootstrap; only the document-construction differs.
  - Replace the react raw-code fallback in `ArtifactPreview.tsx` with the live
    preview once the vendored bundle is in place; keep the raw-code fallback for
    compile errors (Req 7.1).
  - NOTE: if this work moves the preview to a document SERVED FROM OUR OWN ORIGIN
    (rather than a raw `srcdoc` iframe), revisit whether `frame-ancestors 'none'`
    can be delivered as a real HTTP RESPONSE HEADER at that point — via `<meta>` it
    is currently a no-op (see design §5 honest caveat), but header delivery would
    make it genuinely active.
  - _Requirements: 3.1, 3.2, 4.1, 4.2, 4.3, 4.4, 4.5_

## Notes

- **Test policy (lean, per directive):** only the parser unit tests (Task 4) and
  the focused egress security check (Task 8) are MANDATORY required checkboxes.
  Everything else testing-related (Tasks 9, 10) is `- [ ]*` optional. No
  property-based testing is used for this stage.
- The two load-bearing resolutions are implemented explicitly: the static/vendored
  Sandpack client (no remote bundler, no npm fetch — design §7) and the in-sandbox
  honest egress verification incl. the form-action/navigation vector (design §5/§6).
- The `form-action 'none'`, `frame-ancestors 'none'`, and `base-uri 'none'`
  directives are non-negotiable and explicit (they do not inherit from
  `default-src`), closing the auto-submitting-form exfiltration hole.
- Backend touch is minimal (toggle + prompt instruction); no backend endpoint ever
  compiles or executes generated code.
- **Scope decision (deliberate):** Phase 4 ships `html`-only live preview. The
  `react` template shows an honest raw-code fallback with a visible
  known-limitation note; the live React preview is tracked as Task 12 (vendored
  bundle per design §7). This is an intentional, visible deferral — NOT silent
  drift from design.md's two-template spec.
- **Egress counting (honesty):** the in-sandbox sink wrappers count a blocked
  attempt ONLY when the underlying call actually fails/rejects (or a form submit
  is prevented / a securitypolicyviolation fires), NOT unconditionally on every
  call — so the indicator stays honest even if the CSP is ever changed.

## Post-Phase-4 fixes (edge cases from end-to-end runs)

These five fixes were made AFTER Phase 4 shipped, in response to bugs surfaced by
live end-to-end runs. Recorded here so they are not undocumented drift. Each entry
states what changed, the exact test coverage, and — honestly — where coverage is
absent or an assumption is still untested.

### Fix 1 — NER tech-stack allowlist (redactor.py)
- WHAT: `NerDetector.detect` now skips emitting a `person` span for any entity
  whose text is an allowlisted tech term (HTML/CSS/JavaScript/React/Vue/Angular/
  Node.js/... — `TECH_STACK_ALLOWLIST`). Per-entity, case-insensitive. Only gates
  NER `person` spans; regex + custom-term detectors untouched.
- TESTS: `backend/tests/test_ner_tech_allowlist.py` —
  `test_tech_terms_skipped_but_person_kept` (tech ents → no span, "John Doe" → one
  person span) and `test_tech_survives_while_pii_is_redacted` (end-to-end redact():
  tech verbatim; email/SSN/John Doe redacted; person count == 1).
- UNTESTED GAP: a genuine name IMMEDIATELY ADJACENT to a tech term with no
  separator (e.g. "Angular/JohnDoe") is NOT covered. The tests inject
  pre-segmented fake entities, so they bypass the real spaCy tokenizer/entity
  boundaries. If the real model merged "Angular/John Doe" into ONE entity, the
  allowlist would not match the merged string and it would be redacted whole —
  sound in principle, but this depends on real spaCy behavior we have not
  exercised with a test.

### Fix 2 — Markdown fence fallback + BUILD-mode gate (frontend/lib/artifactParser.ts)
- WHAT: added a SECONDARY fence fallback (```html/css/js opens a live artifact when
  no custom `<<<TOKENQUICK_ARTIFACT>>>` marker appears), then GATED it to BUILD
  task-mode via `setBuildMode()` so PERFORM explanatory snippets never wake the
  iframe. The custom marker stays authoritative and mode-independent.
- TESTS: `frontend/lib/artifactParser.test.ts` — the "fence fallback" groups
  (open/close, allowlist+normalization, straddle, precedence, unclosed) and the
  "fence-fallback gate" group (PERFORM ```html snippet stays OUTSIDE / not
  previewed = the load-bearing regression test; BUILD opens it; custom marker
  works in PERFORM; PERFORM straddle never opens). Custom-marker + body-opacity
  Phase-4 tests unchanged. 42 parser tests + 15 security = 57 total.
- KNOWN CAVEAT (BUILD first-fence-wins, plain wording): within a BUILD generation
  the parser still cannot distinguish the intended deliverable from an
  illustrative fenced snippet — it takes the FIRST allowlisted fence. In practice
  a BUILD answer's first ```html block is overwhelmingly the deliverable, and the
  strengthened BUILD prompts push toward a single wrapped artifact, so this is
  low-risk — but NOT zero. If a BUILD answer led with an illustrative snippet
  before the real artifact, the snippet would preview. The fully robust fix is the
  model reliably emitting the custom marker (which suppresses the fence path).
- UNTESTED ASSUMPTION: the BUILD first-fence-wins gap has NOT been observed against
  the real llama3.2:3b model. "Low risk in practice" is currently an inference
  from the prompt design, not an end-to-end-verified fact.

### Fix 3 — PERFORM-path token limit 2048 → 4096 (main.py)
- WHAT: the PERFORM-path `invoke_sync(max_tokens=...)` was raised 2048 → 4096 so a
  standalone interactive HTML tool generated on the PERFORM path completes instead
  of truncating mid-file. (The BUILD path already used SUBTASK_MAX_TOKENS=8192.)
- TESTS: NONE. This is a config-only change with no new test. Verifying "an
  artifact that truncated at 2048 now completes at 4096" would require a live
  llama3.2:3b generation, which the suite does not do (model calls are
  mocked/skipped). Verified only by reasoning + the full suite still passing.

### Fix 4 — Standalone CSP-safe HTML artifact prompts (main.py + task_router.py)
- WHAT: strengthened `_ARTIFACT_DELIMITER_INSTRUCTION` and the router
  `DECOMPOSER_SYSTEM` / `SUBTASK_SYSTEM` prompts to require a single self-contained
  standalone HTML doc (inline style/script, no CDNs/Babel/JSX) wrapped in the exact
  `<<<TOKENQUICK_ARTIFACT:html>>>` markers, so it runs under the iframe's strict
  CSP. Prompt-string-only; the streaming generator's frame ordering is unchanged.
- TESTS: NONE directly. No test references `inject_artifact_instruction` or the
  delimiter constant. There is NO test confirming (a) the injection's pipeline
  position relative to the redaction stage (redaction runs on the summary →
  gen_redacted; the delimiter instruction is prepended to the COMPRESSED prompt on
  the BUILD path, downstream of redaction — verified by code reading only), or
  (b) that stacking redaction + compression + the delimiter instruction does not
  scramble/conflict. This formal test was the deferred optional Task 9 of this
  spec (`- [ ]*`), so it is a KNOWN, intentional gap — not silent drift.

### Fix 5 — .gitignore for runtime JSONL logs
- WHAT: added `*.jsonl` + `backend/logs/*.jsonl` with a `!backend/logs/.gitkeep`
  exception (and created `.gitkeep`) so runtime telemetry (redaction audit, cache
  decisions, power log, OTEL traces) is never tracked while the logs/ dir persists
  in fresh checkouts.
- TESTS: N/A (repo hygiene). Verified via `git check-ignore`.

### Follow-ups — IMPLEMENTED (with real outcomes)

1. **Guarded real-spaCy adjacency test for Fix 1** → `backend/tests/
   test_ner_tech_allowlist.py::TestRealModelAdjacency` (skips if the model
   artifact is absent). **This caught a real bug.** The real spaCy model
   tokenizes `React/Vue/Angular` and `HTML/CSS/JavaScript` as a SINGLE
   slash-joined entity, and the original allowlist only matched standalone
   tokens — so the exact `HTML/CSS/JavaScript or React/Vue/Angular` pattern from
   the ORIGINAL bug report was still being scrubbed to ⟦REDACTED_PERSON⟧. Fix 1
   was therefore INCOMPLETE. FIXED by adding "Candidate 3" to
   `_is_allowlisted_tech` in `redactor.py`: split a joined entity on `/ , | + &`
   and whole-word `or`/`and`, and treat it as allowlisted ONLY if EVERY component
   is a tech term (so `React/Vue/Angular` is skipped, but a mixed
   `Angular/John Doe` is still redacted whole). All 3 real-model tests now pass.

2. **Backfill of the deferred Task 9 injection-ordering test** →
   `backend/tests/test_artifact_injection_ordering.py` (3 tests). Confirms the
   delimiter instruction sits DOWNSTREAM of redaction+compression (prepended to
   the compressed prompt, body preserved verbatim, markers artifact-first),
   disabled=passthrough, and that stacking redaction+compression+injection
   preserves placeholders and leaks none of the regex-redactable secrets. (Note:
   this unit wires only the REGEX detectors, so a person name is out of scope
   here — NER-based name redaction is covered by the NER test above.)

3. **Real-model check of the BUILD first-fence-wins concern (Fix 2)** — a
   one-off lightweight probe (not committed) fired 3 real `llama3.2:3b` BUILD
   generations with the actual artifact-delimiter instruction. RESULT: all 3 led
   with the custom `<<<TOKENQUICK_ARTIFACT:html>>>` marker at position 0 and
   emitted ZERO Markdown fences — so the fence fallback was never reached and the
   "first illustrative fence" case did not occur. This converts "low risk in
   practice" from assumption to OBSERVATION for the common case. Honest caveat:
   3 non-adversarial samples with the current prompt; it shows the model follows
   the custom-marker instruction well for straightforward tool requests (the
   condition under which the fence fallback stays dormant), NOT a universal
   guarantee for complex/edge prompts. The BUILD first-fence-wins gap in the
   fence fallback remains as documented under Fix 2, now with evidence that the
   antecedent is uncommon in practice.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1", "3"] },
    { "id": 1, "tasks": ["2.1", "2.2", "4"] },
    { "id": 2, "tasks": ["5"] },
    { "id": 3, "tasks": ["6.1", "6.2", "6.3"] },
    { "id": 4, "tasks": ["7.1", "7.2"] },
    { "id": 5, "tasks": ["8", "9", "10"] },
    { "id": 6, "tasks": ["11"] },
    { "id": 7, "tasks": ["12"] }
  ]
}
```
