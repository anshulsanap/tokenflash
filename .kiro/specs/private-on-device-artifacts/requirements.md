# Requirements Document

## Introduction

This feature adds a **live, in-browser preview pane** for code the local model
generates on the BUILD path. When a generation produces a renderable web
artifact (an HTML/CSS/JS or React component), the frontend extracts it from the
token stream and renders a running preview using **Sandpack** — an in-browser
bundler that compiles and previews code inside a sandboxed iframe via a
WebWorker, with **no server round-trip and no build toolchain on the user's
machine**.

The stage is a natural fit for TokenQuick's "100% local, $0 API cost, private"
posture: the model runs locally, and now its output *runs* locally too, inside a
sandbox whose outbound network access is restricted so generated code cannot
exfiltrate anything. It mirrors the existing pipeline stages (redaction, semantic
cache, power telemetry): an independently toggleable stage that adds a
presentational panel and streams/participates in the existing generate flow
without disturbing the other stages.

Unlike the redaction and power-telemetry stages, this is **UI/integration work,
not a privacy-critical backend subsystem**. The one genuinely
security-sensitive surface here is the sandbox's network-egress restriction
(Requirement 4) and its honest verification (Requirement 5); those get the
scrutiny, while the rest is covered by lightweight unit tests on the parser
state machine rather than heavy property-based testing.

## Glossary

- **Artifact** — a single, self-contained block of renderable code the model
  emitted, delimited by the artifact markers, intended for live preview.
- **Artifact_Delimiter** — the unambiguous open/close marker pair that brackets
  an artifact in the token stream (see Requirement 1), chosen to avoid collision
  with Markdown code fences (backticks) the model routinely emits.
- **Streaming_Parser** — the frontend state machine that scans the incremental
  Vercel AI SDK token stream and extracts artifact code as it arrives, tolerating
  a not-yet-complete block (see Requirement 2).
- **Primary_Artifact** — the first artifact block detected in a single
  generation; the only block that is rendered as a live preview (see
  Requirement 9).
- **Sandbox** — the Sandpack-managed sandboxed iframe (plus its WebWorker
  bundler) in which the Primary_Artifact is compiled and previewed.
- **Egress_Restriction** — the mechanism (CSP `connect-src 'none'` and/or iframe
  `sandbox` attributes) that blocks the Sandbox from making outbound network
  requests (see Requirement 4).
- **Egress_Signal** — a runtime signal (e.g. a CSP violation event) that fires
  when a blocked outbound attempt actually occurs, used to *verify* — not merely
  assert — that the Egress_Restriction took effect (see Requirement 5).
- **Incomplete_Block_Timeout** — the elapsed-time threshold after which a still
  unclosed artifact block is classified as abandoned/malformed and falls back to
  raw code (see Requirement 7).
- **Stage_Toggle** — the independent on/off switch for this stage, mirroring the
  redaction / cache / power-telemetry toggles (see Requirement 8).

---

## Requirements

### Requirement 1: Unambiguous artifact delimiters

**User Story:** As a developer previewing generated code, I want artifacts marked
with unambiguous delimiters, so that backticks inside the generated code never
confuse the parser about where the artifact begins or ends.

#### Acceptance Criteria

1.1. WHEN the generation prompt/system instruction is constructed for the BUILD
path with this stage enabled, THEN the system SHALL instruct the model to wrap a
renderable artifact in a delimiter pair of the form
`<<<TOKENQUICK_ARTIFACT:lang>>>` … `<<<END_ARTIFACT>>>`, where `lang` is a
short language/template tag (e.g. `html`, `react`).

1.2. THE Artifact_Delimiter markers SHALL be strings that do not occur in
ordinary Markdown code fences, so a triple-backtick block (```` ``` ````) inside
the artifact body is treated as artifact content, never as a delimiter.

1.3. WHEN the model emits triple backticks, language hints, or nested code fences
*inside* the delimited artifact body, THEN the Streaming_Parser SHALL treat them
as literal artifact content and SHALL NOT end the block on them.

1.4. THE open marker SHALL carry the language/template tag and THE parser SHALL
extract that tag to select the Sandpack template; WHEN the tag is missing or
unrecognized, THEN the system SHALL apply a documented default template rather
than failing.

1.5. WHERE the model does not emit any Artifact_Delimiter in a generation, THE
system SHALL treat the generation as having no artifact and SHALL leave the
existing code/text output path unchanged (no preview pane content).

### Requirement 2: Incremental streaming parser (state machine)

**User Story:** As a user watching a generation stream in, I want the preview to
build up incrementally without the UI crashing on a half-received block, so that
streaming stays smooth.

#### Acceptance Criteria

2.1. THE Streaming_Parser SHALL be a state machine over the incremental token
stream with at least the states: `OUTSIDE` (no artifact open), `IN_ARTIFACT`
(open marker seen, close marker not yet seen), and `CLOSED` (close marker seen).

2.2. WHILE in the `IN_ARTIFACT` state, THE parser SHALL expose the
partial artifact code accumulated so far, updating it as new tokens arrive.

2.3. WHEN a token chunk splits an Artifact_Delimiter across two stream chunks
(the marker straddles a chunk boundary), THEN the parser SHALL buffer the partial
marker and correctly recognize the complete marker once the remainder arrives,
without emitting a false open/close.

2.4. WHILE an artifact block is still open (`IN_ARTIFACT`) and not yet timed out,
THE UI SHALL NOT crash, throw an unhandled error, or discard the partial content;
it SHALL render a "still streaming" affordance (e.g. the partial code and/or a
streaming indicator).

2.5. WHEN the close marker is received, THEN the parser SHALL transition to
`CLOSED`, finalize the Primary_Artifact code, and signal that the artifact is
ready to render.

2.6. THE parser SHALL be implemented as pure, synchronous, side-effect-free
transition logic that can be unit-tested on fixed input chunk sequences
independently of React and Sandpack (supports the lean testing plan).

### Requirement 3: In-browser Sandpack rendering

**User Story:** As a user, I want the generated artifact compiled and previewed
right in the browser, so that I see it running with no server round-trip and no
local build toolchain.

#### Acceptance Criteria

3.1. WHEN a Primary_Artifact reaches the `CLOSED` (ready) state AND the
Stage_Toggle is on, THEN the system SHALL render it in a Sandpack preview that
compiles/bundles the code in-browser via a WebWorker.

3.2. THE Sandpack template SHALL be selected from the artifact's language tag
(Requirement 1.4), supporting at minimum a static HTML/CSS/JS template and a
React component template, with a documented default.

3.3. THE preview SHALL render inside a sandboxed iframe (the Sandbox); no
artifact code SHALL execute in the top-level app context.

3.4. THE Sandpack preview SHALL run entirely client-side; the feature SHALL NOT
add any backend endpoint that compiles, executes, or evaluates generated code.

### Requirement 4: Network-egress restriction (CRITICAL SECURITY REQUIREMENT)

**User Story:** As a security-conscious user, I want the sandbox to be prevented
from making outbound network requests, so that generated code cannot exfiltrate
data from my machine or call out to third parties.

#### Acceptance Criteria

4.1. THE Sandbox SHALL be configured with an Egress_Restriction that blocks
outbound network requests originating from the previewed artifact — via a
Content-Security-Policy `connect-src 'none'` (and correspondingly restrictive
`default-src`/`img-src`/`font-src` as needed) and/or restrictive iframe `sandbox`
attributes — as a **prevention** mechanism that stops requests before they leave.

4.2. THE Egress_Restriction SHALL block, at minimum, the following outbound
vectors originating from artifact code: `fetch`, `XMLHttpRequest`, `WebSocket`,
`EventSource`, `navigator.sendBeacon`, AND navigation-based exfiltration —
specifically HTML **form submission** to an external target (e.g. an
auto-submitting `<form action="https://…">`) and other document navigations that
carry data off-origin. A blocked vector is a navigation, not only a
fetch/XHR/WebSocket call, so the restriction SHALL cover form submission and
navigation as first-class exfiltration vectors, not merely the scripting APIs.

4.3. THE Egress_Restriction SHALL be applied to the Sandbox regardless of the
artifact's content, and SHALL NOT be relaxed or widened by anything in the
generated code.

4.4. THE Egress_Restriction SHALL NOT depend on any allowlist that includes
third-party origins; the default posture SHALL be "deny outbound."

4.5. IF the Sandpack/WebWorker runtime itself requires specific local origins to
function (e.g. the bundler's own worker/asset origin), THEN those SHALL be scoped
as narrowly as the runtime permits and documented, and SHALL NOT amount to a
blanket outbound allow.

### Requirement 5: "0 Network Calls" indicator: honest verification, not a config check

**User Story:** As a user who is told "0 network calls," I want that claim backed
by an actual runtime signal that would fire if the sandbox tried to call out, so
that the indicator reflects verified behavior rather than merely a
configuration-present check.

#### Acceptance Criteria

5.1. THE system SHALL distinguish, in both wording and mechanism, between two
different things: (a) the **prevention** mechanism of Requirement 4 (the CSP /
sandbox blocks requests before they happen), and (b) the **verification**
mechanism of this requirement (a runtime signal that proves whether any blocked
outbound attempt actually occurred). These SHALL NOT be conflated.

5.2. WHERE Sandpack's architecture makes a runtime Egress_Signal feasible (for
example, a `securitypolicyviolation` event listener, or another observable
signal emitted when a blocked outbound attempt occurs), THE indicator SHALL be
backed by that active listener: WHEN a blocked outbound attempt occurs, THEN the
signal SHALL be caught and logged, and the indicator SHALL reflect that an
attempt was detected and blocked.

5.3. WHEN no blocked outbound attempt has been observed AND a functioning
Egress_Signal listener is in place, THEN the indicator MAY report a verified "0
outbound calls" state, because a real attempt would have been caught.

5.4. IF no runtime Egress_Signal is feasible with Sandpack's architecture (i.e.
blocked attempts cannot be observed at runtime), THEN the indicator SHALL be
labeled honestly as "restriction configured" (or equivalent) and SHALL NOT be
labeled "0 calls verified" or any wording implying dynamic verification.

5.5. THE indicator SHALL NEVER present a static check that the restrictive CSP /
sandbox attributes are merely *present in configuration* as though it were
dynamic runtime verification.

5.6. **OPEN DESIGN QUESTION (for the design phase to resolve):** Whether a
reliable runtime Egress_Signal is achievable within Sandpack's iframe/WebWorker
architecture — specifically whether `securitypolicyviolation` events (or an
equivalent) from the sandboxed iframe are observable by the host app, and if not,
which honest fallback labeling from 5.4 applies. The design document SHALL
resolve this explicitly and choose either the verified-signal path (5.2/5.3) or
the honestly-labeled configured path (5.4), with justification. This requirement
SHALL NOT be considered satisfied by a design that quietly reduces it to a
config-present check.

### Requirement 6: Intent routing into the existing BUILD path

**User Story:** As a user, I want the preview to appear only for build-type
generations, so that a "perform a task" (research/notes) result is not forced into
a code preview.

#### Acceptance Criteria

6.1. THE artifact-preview stage SHALL hook into the existing BUILD path of the
task router; WHEN the intent classifier routes a request to PERFORM (a knowledge
task), THEN no artifact extraction or preview SHALL be attempted.

6.2. WHEN this stage is enabled AND the request is routed to BUILD, THEN the
system SHALL apply the artifact delimiter instruction (Requirement 1.1) and run
the Streaming_Parser over the generated stream.

6.3. THE stage SHALL integrate with the existing generate/stream flow (the Vercel
AI SDK data-stream protocol and the `generate` phase) without changing the
behavior of the redaction, semantic-cache, or power-telemetry stages.

### Requirement 7: Malformed / incomplete code handling with an explicit timeout

**User Story:** As a user, I want a graceful fallback to raw code when a block is
never closed or fails to compile, and I do not want a slow-but-healthy stream
mistaken for an abandoned one.

#### Acceptance Criteria

7.1. WHEN an artifact block is closed but the Sandpack compilation/bundle fails,
THEN the system SHALL fall back to showing the raw artifact code (and a brief
error affordance) rather than crashing or showing a blank preview.

7.2. THE system SHALL explicitly distinguish two conditions for an unclosed block:
(a) **"still streaming, wait"** — the overall token stream is still active and
tokens are still arriving; and (b) **"abandoned/malformed, fall back"** — the
Incomplete_Block_Timeout has elapsed or the stream has ended without a close
marker.

7.3. WHILE the token stream is still active (new tokens still arriving) AND the
Incomplete_Block_Timeout has not elapsed, THE system SHALL remain in the "still
streaming, wait" condition and SHALL NOT fall back — a slow response SHALL NOT be
misclassified mid-generation.

7.4. WHEN the overall generation stream ends (the `d:` finish frame arrives or the
stream closes) with an artifact block still open, THEN the system SHALL classify
the block as abandoned/malformed and SHALL fall back to raw code.

7.5. WHEN an artifact block has been open with no new tokens for longer than the
Incomplete_Block_Timeout (a stall, distinct from steady slow streaming), THEN the
system SHALL classify the block as abandoned/malformed and SHALL fall back to raw
code. THE design phase SHALL specify the concrete timeout value and whether it is
measured as time-since-last-token (idle/stall) versus wall-clock-since-open, with
the intent that steady slow streaming is preserved and only a genuine stall or
stream-end triggers fallback.

7.6. WHEN the fallback to raw code is triggered, THEN the raw artifact code SHALL
be shown verbatim (no partial execution), preserving whatever content was
received.

### Requirement 8: Independent stage toggle

**User Story:** As a user, I want to turn the live preview on or off independently,
just like the redaction, cache, and power-telemetry stages.

#### Acceptance Criteria

8.1. THE stage SHALL expose an independent Stage_Toggle, mirroring the existing
redaction / semantic-cache / power-telemetry toggles, with a default state
specified by the design (consistent with the other stages).

8.2. WHEN the Stage_Toggle is off, THEN the system SHALL NOT inject the artifact
delimiter instruction, SHALL NOT run the Streaming_Parser preview, and SHALL NOT
render a Sandpack preview; the generate flow SHALL behave exactly as it does today
with no preview stage.

8.3. THE Stage_Toggle SHALL be readable and settable through the same
UI/settings surface pattern used by the other stage toggles, and its state SHALL
apply to generations that BEGIN after the change (in-flight generations keep their
snapshot), consistent with the other stages.

8.4. THE Stage_Toggle SHALL be independent: toggling it SHALL NOT alter the
enabled/disabled state or behavior of redaction, semantic cache, or power
telemetry.

### Requirement 9: Multiple-blocks policy

**User Story:** As a user, I want predictable behavior when the model emits more
than one artifact block, so that additional blocks are handled explicitly rather
than causing undefined behavior.

#### Acceptance Criteria

9.1. WHEN a single generation contains more than one artifact block, THEN the
system SHALL render only the Primary_Artifact (the first detected block) as the
live Sandpack preview.

9.2. THE handling of additional (non-primary) artifact blocks SHALL be explicit
and specified by the design as ONE of: (a) silently dropped from the preview, or
(b) shown as plain (non-executed) code beneath the rendered Primary_Artifact.
This behavior SHALL NOT be left undefined.

9.3. WHERE additional blocks are shown as plain code (option 9.2b), THEN they
SHALL NOT be compiled or executed in the Sandbox; only the Primary_Artifact runs.

9.4. THE selection of the Primary_Artifact SHALL be deterministic (the first
block whose open marker is encountered in stream order).

### Requirement 10: Redaction interaction is out of scope

**User Story:** As a maintainer, I want the boundary with the redaction stage to
be explicit, so that redaction placeholders in generated code are handled
predictably and this stage does not attempt any un-redaction.

#### Acceptance Criteria

10.1. THE interaction between this stage and the pre-inference redaction stage
SHALL be explicitly OUT OF SCOPE: this stage SHALL NOT reverse, resolve, or
otherwise "un-redact" any redaction placeholder.

10.2. WHEN generated code contains a redaction placeholder (e.g.
`⟦REDACTED_<CATEGORY>⟧`), THEN the artifact SHALL render that placeholder verbatim
as literal mock text in the preview, exactly as it appears in the code.

10.3. THE stage SHALL NOT add any dependency on, or coupling to, redaction state;
placeholders are treated as ordinary characters in the artifact body.

---

## Non-functional & scope notes

- **Testing posture (lean, per directive):** This is UI/integration work, not a
  privacy-critical subsystem. The mandatory automated tests SHALL be lightweight
  **unit tests for the Streaming_Parser state transitions** (Requirement 2) —
  covering `OUTSIDE → IN_ARTIFACT → CLOSED`, split-marker buffering (2.3),
  backticks-inside-body (1.3), no-artifact streams (1.5), stream-end-while-open
  (7.4), and multiple-blocks primary selection (9.4). Heavy property-based
  testing is explicitly NOT required for this stage. The security-sensitive
  surfaces (Requirements 4 and 5) SHALL be validated by the design's chosen
  verification mechanism plus a focused test/manual check of the Egress_Signal
  path, not by broad PBT.
- **Client-side only:** No backend endpoint compiles or executes generated code;
  the only backend touch is the BUILD-path prompt instruction and (if needed) the
  Stage_Toggle surface, consistent with the existing stage-toggle pattern.
- **No new outbound network dependency at runtime for the feature itself** beyond
  what Sandpack's in-browser bundler inherently requires; any such requirement is
  scoped and documented under Requirement 4.5.

## Open questions for the design phase

1. **(Requirement 5.6 — load-bearing)** Is a runtime Egress_Signal (e.g.
   `securitypolicyviolation` events from the sandboxed iframe) observable by the
   host app within Sandpack's architecture? If yes, design the verified-signal
   indicator (5.2/5.3). If no, design the honestly-labeled "restriction
   configured" indicator (5.4). Resolve explicitly — do not reduce to a config
   check.
2. **(Requirement 4 — load-bearing)** Does Sandpack's default bundler mode
   require fetching npm package dependencies over the network (e.g. via a hosted
   bundler service such as the default remote bundler), and if so, is that
   compatible with Requirement 4's "deny outbound, no allowlist" posture? If it
   is NOT compatible, the design phase MUST decide explicitly between: (a)
   restricting supported templates to ones with no external package resolution
   (vanilla HTML/CSS/JS, or a fully vendored / pre-bundled React that resolves no
   packages over the network); or (b) accepting a narrowly-scoped, explicitly
   documented exception to Requirement 4 for the bundler's own dependency-fetch
   traffic specifically — and justifying whichever is chosen. This MUST be
   resolved explicitly in design.md, NOT left implicit in Requirement 4.5.
3. **(Requirement 7.5)** Concrete Incomplete_Block_Timeout value and whether it is
   measured as idle-since-last-token (stall) vs wall-clock-since-open, chosen so
   steady slow streaming is never misclassified.
4. **(Requirement 9.2)** Choose silently-dropped vs shown-as-plain-code for
   additional blocks.
5. **(Requirement 1.4 / 3.2)** The set of supported Sandpack templates and the
   default when the language tag is missing/unrecognized.
6. **(Requirement 8.1)** Default on/off state for the Stage_Toggle, consistent
   with the other stages.
