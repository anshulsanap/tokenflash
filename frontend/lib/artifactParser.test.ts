// Mandatory unit tests for the streaming artifact parser (Task 4).
//
// Lightweight, example-based unit tests over FIXED chunk sequences — NO
// property-based testing, per the lean testing directive (design Testing
// Strategy). The parser is pure and side-effect-free, so these run in a plain
// `node` environment with no DOM.
//
// Coverage maps to the design Testing Strategy cases and Requirements
// 1.3, 1.4, 1.5, 2.3, 2.4, 2.5, 7.4, 9.4.

import { describe, it, expect } from "vitest";
import {
  ArtifactStreamParser,
  OPEN_PREFIX,
  OPEN_SUFFIX,
  CLOSE_MARKER,
  type ParseResult,
} from "./artifactParser";

// --- helpers -------------------------------------------------------------

// Build a complete open marker for a given lang tag.
function open(lang: string): string {
  return OPEN_PREFIX + lang + OPEN_SUFFIX;
}

// Feed a whole string as a single push and finalize the stream.
function feedWhole(text: string): ParseResult {
  const p = new ArtifactStreamParser();
  p.push(text);
  return p.onStreamEnd();
}

// Feed an array of chunks in order and finalize the stream.
function feedChunks(chunks: string[]): ParseResult {
  const p = new ArtifactStreamParser();
  for (const c of chunks) p.push(c);
  return p.onStreamEnd();
}

// BUILD-mode variants: the fence fallback is gated on BUILD task-mode, so these
// helpers put the parser in build mode BEFORE feeding — used by every
// fence-fallback test (the custom-marker tests must NOT use these, proving the
// custom path is mode-independent).
function feedWholeBuild(text: string): ParseResult {
  const p = new ArtifactStreamParser();
  p.setBuildMode(true);
  p.push(text);
  return p.onStreamEnd();
}

function feedChunksBuild(chunks: string[]): ParseResult {
  const p = new ArtifactStreamParser();
  p.setBuildMode(true);
  for (const c of chunks) p.push(c);
  return p.onStreamEnd();
}

// =========================================================================
// Case 1: Happy path OUTSIDE -> IN_ARTIFACT -> CLOSED for a single block.
// (Req 2.1, 2.5)
// =========================================================================
describe("happy path: single block OUTSIDE -> IN_ARTIFACT -> CLOSED", () => {
  it("observes each state transition as chunks arrive", () => {
    const p = new ArtifactStreamParser();

    // Before any marker we are OUTSIDE.
    let snap = p.push("Here is your component:\n");
    expect(snap.state).toBe("OUTSIDE");
    expect(snap.sawAnyMarker).toBe(false);

    // Open marker moves us IN_ARTIFACT.
    snap = p.push(open("html"));
    expect(snap.state).toBe("IN_ARTIFACT");
    expect(snap.lang).toBe("html");
    expect(snap.sawAnyMarker).toBe(true);
    expect(snap.primaryClosed).toBe(false);

    // Body accumulates while IN_ARTIFACT.
    snap = p.push("<h1>Hello</h1>");
    expect(snap.state).toBe("IN_ARTIFACT");
    expect(snap.primaryCode).toBe("<h1>Hello</h1>");

    // Close marker finalizes the block.
    snap = p.push(CLOSE_MARKER);
    expect(snap.state).toBe("CLOSED");
    expect(snap.primaryClosed).toBe(true);
    expect(snap.primaryCode).toBe("<h1>Hello</h1>");

    // onStreamEnd keeps the CLOSED result stable.
    const final = p.onStreamEnd();
    expect(final.state).toBe("CLOSED");
    expect(final.primaryClosed).toBe(true);
    expect(final.lang).toBe("html");
    expect(final.primaryCode).toBe("<h1>Hello</h1>");
    expect(final.extraBlocks).toEqual([]);
  });
});

// =========================================================================
// Case 2: Split-marker buffering (Req 2.3) — the load-bearing straddle test.
// Feed the OPEN marker and the CLOSE marker split across TWO push() calls at
// EVERY byte offset; assert no false open/close and identical final result to
// the whole-feed case.
// =========================================================================
describe("split-marker buffering across chunk boundaries (Req 2.3)", () => {
  const lang = "react";
  const body = "export default function App(){return <div>hi</div>}";
  const openMarker = open(lang);
  const wholeStream = "prose before " + openMarker + body + CLOSE_MARKER + " prose after";

  // Reference result: feeding the whole stream in one push.
  const reference = feedWhole(wholeStream);

  it("reference whole-feed produces a CLOSED primary with the expected code", () => {
    expect(reference.state).toBe("CLOSED");
    expect(reference.primaryClosed).toBe(true);
    expect(reference.lang).toBe(lang);
    expect(reference.primaryCode).toBe(body);
  });

  it("splitting the OPEN marker at every byte offset never yields a false open and matches the reference", () => {
    const prefix = "prose before ";
    const suffix = body + CLOSE_MARKER + " prose after";
    for (let i = 1; i < openMarker.length; i++) {
      const chunkA = prefix + openMarker.slice(0, i);
      const chunkB = openMarker.slice(i) + suffix;

      const p = new ArtifactStreamParser();
      // After the first (partial-marker) push, the parser must NOT have falsely
      // opened an artifact from an incomplete open marker.
      const mid = p.push(chunkA);
      expect(
        mid.state === "OUTSIDE" || mid.state === "IN_ARTIFACT"
      ).toBe(true);
      // The partial open marker text must not leak into primaryCode.
      expect(mid.primaryCode).toBe("");

      p.push(chunkB);
      const final = p.onStreamEnd();
      expect(final.state, `open split at offset ${i}`).toBe("CLOSED");
      expect(final.primaryClosed, `open split at offset ${i}`).toBe(true);
      expect(final.lang, `open split at offset ${i}`).toBe(lang);
      expect(final.primaryCode, `open split at offset ${i}`).toBe(body);
    }
  });

  it("splitting the CLOSE marker at every byte offset never yields a false close and matches the reference", () => {
    const head = "prose before " + openMarker + body;
    const tail = " prose after";
    for (let i = 1; i < CLOSE_MARKER.length; i++) {
      const chunkA = head + CLOSE_MARKER.slice(0, i);
      const chunkB = CLOSE_MARKER.slice(i) + tail;

      const p = new ArtifactStreamParser();
      // After the first push the close marker is only partial: the block must
      // still be open, and the partial close marker must NOT be in primaryCode.
      const mid = p.push(chunkA);
      expect(mid.state, `close split at offset ${i}`).toBe("IN_ARTIFACT");
      expect(mid.primaryClosed, `close split at offset ${i}`).toBe(false);
      expect(mid.primaryCode, `close split at offset ${i}`).toBe(body);

      p.push(chunkB);
      const final = p.onStreamEnd();
      expect(final.state, `close split at offset ${i}`).toBe("CLOSED");
      expect(final.primaryClosed, `close split at offset ${i}`).toBe(true);
      expect(final.primaryCode, `close split at offset ${i}`).toBe(body);
    }
  });

  it("splitting into single-character chunks (extreme straddle) still matches the reference", () => {
    const chars = wholeStream.split("");
    const final = feedChunks(chars);
    expect(final.state).toBe("CLOSED");
    expect(final.primaryClosed).toBe(true);
    expect(final.lang).toBe(lang);
    expect(final.primaryCode).toBe(body);
  });
});

// =========================================================================
// Case 3: Body opacity (Req 1.3). Body contains triple-backtick fences AND a
// near-miss "<<<END" string that is NOT the full CLOSE_MARKER; the block only
// closes on the exact CLOSE_MARKER, and backticks/near-miss are preserved
// verbatim.
// =========================================================================
describe("body opacity: fences and near-miss markers are literal content (Req 1.3)", () => {
  it("only the exact CLOSE_MARKER ends the block; backticks and <<<END near-miss are preserved verbatim", () => {
    const body =
      "```js\nconst x = 1;\n```\n" +
      "// a near miss: <<<END and <<<END_ARTIFACT_NOT and <<<TOKENQUICK not real\n" +
      "console.log('```');";
    const stream = open("html") + body + CLOSE_MARKER;

    const final = feedWhole(stream);
    expect(final.state).toBe("CLOSED");
    expect(final.primaryClosed).toBe(true);
    // The body — including backtick fences and the near-miss strings — is
    // preserved byte-for-byte.
    expect(final.primaryCode).toBe(body);
    // Sanity: the near-miss substrings really are still present.
    expect(final.primaryCode).toContain("```");
    expect(final.primaryCode).toContain("<<<END and");
    expect(final.primaryCode).toContain("<<<END_ARTIFACT_NOT");
  });

  it("a bare <<<END without the full close marker does not close the block (stream-end leaves it open)", () => {
    const body = "line one\n<<<END\nline two still inside";
    const p = new ArtifactStreamParser();
    p.push(open("html"));
    p.push(body);
    const final = p.onStreamEnd();
    // Never saw the real CLOSE_MARKER → still open at stream end.
    expect(final.state).toBe("IN_ARTIFACT");
    expect(final.primaryClosed).toBe(false);
    expect(final.primaryCode).toBe(body);
  });
});

// =========================================================================
// Case 4: No-artifact stream (Req 1.5). Plain prose in → sawAnyMarker false and
// primaryCode empty.
// =========================================================================
describe("no-artifact stream (Req 1.5)", () => {
  it("plain prose produces sawAnyMarker=false and empty primaryCode", () => {
    const final = feedWhole(
      "Here are some notes about your task. No code was produced, just an explanation."
    );
    expect(final.sawAnyMarker).toBe(false);
    expect(final.state).toBe("OUTSIDE");
    expect(final.primaryCode).toBe("");
    expect(final.extraBlocks).toEqual([]);
  });

  it("prose containing backticks but no artifact marker is still no-artifact", () => {
    const final = feedChunks(["Use `npm ", "install` and ", "```py\nprint(1)\n``` done"]);
    expect(final.sawAnyMarker).toBe(false);
    expect(final.primaryCode).toBe("");
  });
});

// =========================================================================
// Case 5: Stream-end-while-open (Req 7.4). Open marker + partial body, then
// onStreamEnd(); state remains IN_ARTIFACT (not CLOSED) and the partial body is
// preserved in primaryCode.
// =========================================================================
describe("stream-end-while-open (Req 7.4)", () => {
  it("leaves state IN_ARTIFACT and preserves the partial body when the close marker never arrives", () => {
    const partial = "<div>partially strea";
    const p = new ArtifactStreamParser();
    p.push(open("react"));
    p.push(partial);
    const final = p.onStreamEnd();
    expect(final.state).toBe("IN_ARTIFACT");
    expect(final.primaryClosed).toBe(false);
    expect(final.lang).toBe("react");
    expect(final.primaryCode).toBe(partial);
    expect(final.sawAnyMarker).toBe(true);
  });
});

// =========================================================================
// Case 6: Multiple blocks (Req 9.4). Two complete blocks → the FIRST is
// primaryCode, the second is in extraBlocks; deterministic primary selection;
// state CLOSED.
// =========================================================================
describe("multiple blocks: deterministic primary selection (Req 9.4)", () => {
  it("first block becomes primaryCode, second goes to extraBlocks", () => {
    const first = "<h1>first</h1>";
    const second = "<h2>second</h2>";
    const stream =
      "intro " +
      open("html") + first + CLOSE_MARKER +
      " middle prose " +
      open("react") + second + CLOSE_MARKER +
      " trailing prose";

    const final = feedWhole(stream);
    expect(final.state).toBe("CLOSED");
    expect(final.primaryClosed).toBe(true);
    // Primary is the first opened block; its lang is the first marker's tag.
    expect(final.lang).toBe("html");
    expect(final.primaryCode).toBe(first);
    // The second complete block is captured as an extra, not merged into primary.
    expect(final.extraBlocks).toEqual([second]);
  });

  it("three blocks → primary + two extras in stream order", () => {
    const b1 = "A";
    const b2 = "B";
    const b3 = "C";
    const stream =
      open("html") + b1 + CLOSE_MARKER +
      open("html") + b2 + CLOSE_MARKER +
      open("react") + b3 + CLOSE_MARKER;
    const final = feedWhole(stream);
    expect(final.state).toBe("CLOSED");
    expect(final.primaryCode).toBe(b1);
    expect(final.extraBlocks).toEqual([b2, b3]);
  });
});

// =========================================================================
// Case 7: Language-tag parse (Req 1.4). html, react, empty (-> null), unknown
// tag (passed through as-is).
// =========================================================================
describe("language tag parsing (Req 1.4)", () => {
  it("parses 'html'", () => {
    const final = feedWhole(open("html") + "x" + CLOSE_MARKER);
    expect(final.lang).toBe("html");
  });

  it("parses 'react'", () => {
    const final = feedWhole(open("react") + "x" + CLOSE_MARKER);
    expect(final.lang).toBe("react");
  });

  it("empty tag becomes null", () => {
    const final = feedWhole(open("") + "x" + CLOSE_MARKER);
    expect(final.lang).toBeNull();
  });

  it("unknown tag is passed through verbatim (parser does not validate the set)", () => {
    const final = feedWhole(open("svelte-9") + "x" + CLOSE_MARKER);
    expect(final.lang).toBe("svelte-9");
  });
});

// =========================================================================
// Totality (lightly). push() never throws on arbitrary/garbage chunks and
// always returns a coherent ParseResult with a valid state. (Req 2.4)
// =========================================================================
describe("totality: push never throws and always returns a valid snapshot (Req 2.4)", () => {
  const garbage = [
    "",
    ">>><<<",
    "<<<TOKENQUICK_ARTIFACT:", // dangling open prefix, never terminated
    "<<<END_ARTIFACT>>>", // stray close with no open
    OPEN_PREFIX.slice(0, 5), // partial prefix
    CLOSE_MARKER.slice(0, 7), // partial close
    "\u0000\uFFFF\uD83D\uDE00", // control + emoji surrogate pair
    "<<<TOKENQUICK_ARTIFACT:react>>>",
    "```<<<END",
    "⟦REDACTED_EMAIL⟧", // redaction placeholder is ordinary content
  ];

  const validStates = new Set(["OUTSIDE", "IN_ARTIFACT", "CLOSED"]);

  function assertValid(snap: ParseResult) {
    expect(validStates.has(snap.state)).toBe(true);
    expect(typeof snap.primaryCode).toBe("string");
    expect(typeof snap.primaryClosed).toBe("boolean");
    expect(typeof snap.sawAnyMarker).toBe("boolean");
    expect(Array.isArray(snap.extraBlocks)).toBe(true);
    expect(snap.lang === null || typeof snap.lang === "string").toBe(true);
  }

  it("does not throw for any single garbage chunk", () => {
    for (const g of garbage) {
      const p = new ArtifactStreamParser();
      expect(() => assertValid(p.push(g))).not.toThrow();
      expect(() => assertValid(p.onStreamEnd())).not.toThrow();
    }
  });

  it("does not throw for garbage chunks fed in sequence", () => {
    const p = new ArtifactStreamParser();
    for (const g of garbage) {
      const snap = p.push(g);
      assertValid(snap);
    }
    assertValid(p.onStreamEnd());
  });

  it("a stray close marker with no open never sets primaryClosed", () => {
    const final = feedWhole("no artifact here " + CLOSE_MARKER + " still nothing");
    // Never opened, so the stray close is treated as ordinary text (discarded
    // from the artifact view); no false CLOSED-primary.
    expect(final.state).toBe("OUTSIDE");
    expect(final.primaryClosed).toBe(false);
    expect(final.primaryCode).toBe("");
  });
});

// =========================================================================
// FENCE FALLBACK (Option A: "custom marker preferred; fence fallback only when
// NO custom marker appears"). These are strictly ADDITIVE tests for the
// secondary Markdown code-fence path. The custom-marker tests above must all
// continue to pass unchanged — the fence fallback is suppressed the moment a
// custom marker is (or ever was) seen.
// =========================================================================

describe("fence fallback: opens on ```html and closes on bare ``` (no custom marker, BUILD mode)", () => {
  it("conversational text then ```html ... ``` → CLOSED html, prose NOT in primaryCode", () => {
    const stream =
      "Sure, here is a page:\n" +
      "```html\n<h1>hi</h1>\n```\n" +
      "Let me know if you want changes.";
    const final = feedWholeBuild(stream);
    expect(final.state).toBe("CLOSED");
    expect(final.primaryClosed).toBe(true);
    expect(final.lang).toBe("html");
    expect(final.primaryCode).toBe("<h1>hi</h1>");
    expect(final.sawAnyMarker).toBe(true);
    // Conversational text is not captured as artifact content.
    expect(final.primaryCode).not.toContain("Sure, here is a page");
    expect(final.primaryCode).not.toContain("Let me know");
    expect(final.extraBlocks).toEqual([]);
  });

  it("multi-line body is captured verbatim between the fences", () => {
    const body = "<div>\n  <p>one</p>\n  <p>two</p>\n</div>";
    const final = feedWholeBuild("```html\n" + body + "\n```\n");
    expect(final.state).toBe("CLOSED");
    expect(final.primaryCode).toBe(body);
    expect(final.lang).toBe("html");
  });
});

describe("fence fallback: language allowlist + normalization (BUILD mode)", () => {
  it("```css opens with lang css", () => {
    const final = feedWholeBuild("```css\n.box { color: red; }\n```");
    expect(final.state).toBe("CLOSED");
    expect(final.lang).toBe("css");
    expect(final.primaryCode).toBe(".box { color: red; }");
  });

  it("```javascript opens with lang javascript", () => {
    const final = feedWholeBuild("```javascript\nconsole.log(1)\n```");
    expect(final.state).toBe("CLOSED");
    expect(final.lang).toBe("javascript");
    expect(final.primaryCode).toBe("console.log(1)");
  });

  it("```js is normalized to javascript", () => {
    const final = feedWholeBuild("```js\nconst x = 1\n```");
    expect(final.state).toBe("CLOSED");
    expect(final.lang).toBe("javascript");
    expect(final.primaryCode).toBe("const x = 1");
  });

  it("fence lang is case-insensitive (```HTML) and normalizes", () => {
    const final = feedWholeBuild("```HTML\n<b>x</b>\n```");
    expect(final.state).toBe("CLOSED");
    expect(final.lang).toBe("html");
    expect(final.primaryCode).toBe("<b>x</b>");
  });

  it("tolerates trailing whitespace after the lang on the opening fence line", () => {
    const final = feedWholeBuild("```html   \n<h1>hi</h1>\n```");
    expect(final.state).toBe("CLOSED");
    expect(final.lang).toBe("html");
    expect(final.primaryCode).toBe("<h1>hi</h1>");
  });
});

describe("fence fallback: non-allowlisted lang does NOT open artifact mode (BUILD mode)", () => {
  it("```python stays OUTSIDE / conversational (no artifact)", () => {
    const final = feedWholeBuild("```python\nprint('hi')\n```\nsome prose after");
    expect(final.state).toBe("OUTSIDE");
    expect(final.sawAnyMarker).toBe(false);
    expect(final.primaryCode).toBe("");
    expect(final.extraBlocks).toEqual([]);
  });

  it("a bare ``` with no lang does NOT open artifact mode", () => {
    const final = feedWholeBuild("```\njust a plain fence\n```");
    expect(final.state).toBe("OUTSIDE");
    expect(final.sawAnyMarker).toBe(false);
    expect(final.primaryCode).toBe("");
  });
});

describe("fence fallback: opening-fence straddle across chunk boundaries (BUILD mode)", () => {
  const openFence = "```html\n";
  const body = "<h1>hi</h1>";
  const whole = "intro text\n" + openFence + body + "\n```\ntrailing";
  const reference = feedWholeBuild(whole);

  it("reference whole-feed opens+closes the fenced html block", () => {
    expect(reference.state).toBe("CLOSED");
    expect(reference.lang).toBe("html");
    expect(reference.primaryCode).toBe(body);
  });

  it("splitting the OPEN fence at every byte offset never yields a false open and matches the reference", () => {
    const prefix = "intro text\n";
    const suffix = body + "\n```\ntrailing";
    for (let i = 1; i < openFence.length; i++) {
      const chunkA = prefix + openFence.slice(0, i);
      const chunkB = openFence.slice(i) + suffix;
      const p = new ArtifactStreamParser();
      p.setBuildMode(true);
      const mid = p.push(chunkA);
      // A partial opening fence must NOT falsely open an artifact nor leak into
      // primaryCode.
      expect(
        mid.state === "OUTSIDE" || mid.state === "IN_ARTIFACT",
        `open fence split at offset ${i}`
      ).toBe(true);
      if (mid.state === "OUTSIDE") {
        expect(mid.primaryCode, `open fence split at offset ${i}`).toBe("");
      }
      p.push(chunkB);
      const final = p.onStreamEnd();
      expect(final.state, `open fence split at offset ${i}`).toBe("CLOSED");
      expect(final.lang, `open fence split at offset ${i}`).toBe("html");
      expect(final.primaryCode, `open fence split at offset ${i}`).toBe(body);
    }
  });

  it("single-character chunking (extreme straddle) still matches the reference", () => {
    const final = feedChunksBuild(whole.split(""));
    expect(final.state).toBe("CLOSED");
    expect(final.lang).toBe("html");
    expect(final.primaryCode).toBe(body);
  });
});

describe("fence fallback: closing-fence straddle across chunk boundaries (BUILD mode)", () => {
  const body = "<h1>hi</h1>";
  const closeLine = "\n```"; // newline that ends the content line + the fence
  const head = "```html\n" + body;

  it("splitting the CLOSE fence at every byte offset closes correctly and preserves the body", () => {
    for (let i = 1; i < closeLine.length; i++) {
      const chunkA = head + closeLine.slice(0, i);
      const chunkB = closeLine.slice(i) + "\ntrailing prose";
      const p = new ArtifactStreamParser();
      p.setBuildMode(true);
      const mid = p.push(chunkA);
      // Before the close completes, the block is still open and the partial
      // close characters have not been committed as body.
      expect(mid.state, `close fence split at offset ${i}`).toBe("IN_ARTIFACT");
      expect(mid.primaryClosed, `close fence split at offset ${i}`).toBe(false);
      p.push(chunkB);
      const final = p.onStreamEnd();
      expect(final.state, `close fence split at offset ${i}`).toBe("CLOSED");
      expect(final.primaryClosed, `close fence split at offset ${i}`).toBe(true);
      expect(final.primaryCode, `close fence split at offset ${i}`).toBe(body);
    }
  });
});

describe("fence fallback PRECEDENCE: custom marker wins; fences inside are literal (body opacity)", () => {
  it("a custom artifact whose body contains ``` fences does NOT trigger the fence fallback; fences are literal primaryCode", () => {
    const body =
      "```html\n<h1>inner</h1>\n```\nmore text with ```css\n.x{}\n``` inside";
    const stream = open("html") + body + CLOSE_MARKER;
    const final = feedWhole(stream);
    expect(final.state).toBe("CLOSED");
    expect(final.primaryClosed).toBe(true);
    expect(final.lang).toBe("html");
    // The ENTIRE body — including the ``` fences — is literal content, byte for
    // byte. The fence fallback never fired because the custom marker engaged.
    expect(final.primaryCode).toBe(body);
    expect(final.primaryCode).toContain("```html");
    expect(final.primaryCode).toContain("```css");
    expect(final.extraBlocks).toEqual([]);
  });

  it("once a custom marker has been seen, a LATER ```html fence is NOT treated as a fence open", () => {
    // Custom artifact closes, then conversational text contains a fence. Because
    // a custom marker was already seen, the fence fallback stays suppressed.
    const stream =
      open("html") + "<p>primary</p>" + CLOSE_MARKER +
      "\nfollow-up prose\n```html\n<h1>should be ignored</h1>\n```\n";
    const final = feedWhole(stream);
    expect(final.state).toBe("CLOSED");
    expect(final.lang).toBe("html");
    expect(final.primaryCode).toBe("<p>primary</p>");
    // The later fence did NOT create a primary or an extra block via the fence
    // path (extraBlocks only tracks custom-marker blocks).
    expect(final.extraBlocks).toEqual([]);
  });

  it("a custom OPEN marker that arrives AFTER a fence still uses the fence primary (fence opened first); precedence is about suppression, not retroactive override", () => {
    // No custom marker at the fence-open moment → fence opens the primary
    // (BUILD mode arms the fence fallback).
    const p = new ArtifactStreamParser();
    p.setBuildMode(true);
    p.push("```html\n<h1>fence primary</h1>\n```\n");
    const final = p.onStreamEnd();
    expect(final.state).toBe("CLOSED");
    expect(final.lang).toBe("html");
    expect(final.primaryCode).toBe("<h1>fence primary</h1>");
  });
});

describe("fence fallback: stream ends with an unclosed fenced block (Req 7.4 semantics, BUILD mode)", () => {
  it("open ```html + partial body, no closing fence → IN_ARTIFACT with partial body", () => {
    const p = new ArtifactStreamParser();
    p.setBuildMode(true);
    p.push("```html\n<div>partial");
    const final = p.onStreamEnd();
    expect(final.state).toBe("IN_ARTIFACT");
    expect(final.primaryClosed).toBe(false);
    expect(final.lang).toBe("html");
    expect(final.primaryCode).toBe("<div>partial");
    expect(final.sawAnyMarker).toBe(true);
  });

  it("open ```js + multi-line partial body, no closing fence → IN_ARTIFACT preserves all lines", () => {
    const p = new ArtifactStreamParser();
    p.setBuildMode(true);
    p.push("```js\nline one\nline two\nline three (incomplete");
    const final = p.onStreamEnd();
    expect(final.state).toBe("IN_ARTIFACT");
    expect(final.lang).toBe("javascript");
    expect(final.primaryCode).toBe("line one\nline two\nline three (incomplete");
  });
});

// =========================================================================
// FENCE-FALLBACK GATE (BUILD-mode only). The Markdown-fence fallback may only
// OPEN an artifact when the parser has been told it is in BUILD task-mode. In
// PERFORM / unknown / default mode, ```html/css/js snippets in prose stay
// conversational and never wake the preview. The CUSTOM marker path is
// authoritative and mode-independent (works in perform OR build). These tests
// pin the regression fix: PERFORM explanatory code snippets do not preview.
// =========================================================================
describe("fence-fallback gate: BUILD-mode required to open a fence artifact", () => {
  const snippetStream =
    "Here is an example of the markup:\n" +
    "```html\n<h1>hi</h1>\n```\n" +
    "That's how you'd structure it.";

  it("PERFORM (default, no setBuildMode): a ```html snippet in prose stays OUTSIDE and is NOT previewed", () => {
    // Load-bearing regression test: the exact false-positive the fix addresses.
    const final = feedWhole(snippetStream);
    expect(final.state).toBe("OUTSIDE");
    expect(final.sawAnyMarker).toBe(false);
    expect(final.primaryCode).toBe("");
    expect(final.extraBlocks).toEqual([]);
  });

  it("PERFORM (explicit setBuildMode(false)): same snippet still stays OUTSIDE and is NOT previewed", () => {
    const p = new ArtifactStreamParser();
    p.setBuildMode(false);
    p.push(snippetStream);
    const final = p.onStreamEnd();
    expect(final.state).toBe("OUTSIDE");
    expect(final.sawAnyMarker).toBe(false);
    expect(final.primaryCode).toBe("");
  });

  it("BUILD (setBuildMode(true)): the SAME snippet opens+closes the fence artifact (CLOSED html)", () => {
    const final = feedWholeBuild(snippetStream);
    expect(final.state).toBe("CLOSED");
    expect(final.primaryClosed).toBe(true);
    expect(final.lang).toBe("html");
    expect(final.primaryCode).toBe("<h1>hi</h1>");
    expect(final.sawAnyMarker).toBe(true);
  });

  it("custom marker works in PERFORM mode too (mode-independent): opens+closes normally", () => {
    // buildMode stays false (default) — the custom path must be unaffected.
    const p = new ArtifactStreamParser();
    const stream = open("html") + "<p>perform artifact</p>" + CLOSE_MARKER;
    p.push(stream);
    const final = p.onStreamEnd();
    expect(final.state).toBe("CLOSED");
    expect(final.primaryClosed).toBe(true);
    expect(final.lang).toBe("html");
    expect(final.primaryCode).toBe("<p>perform artifact</p>");
    expect(final.sawAnyMarker).toBe(true);
  });

  it("PERFORM: a ```html fence split across chunks is never opened and never leaks into primaryCode", () => {
    const openFence = "```html\n";
    const body = "<h1>hi</h1>";
    const prefix = "intro prose\n";
    const suffix = body + "\n```\ntrailing prose";
    for (let i = 1; i < openFence.length; i++) {
      const chunkA = prefix + openFence.slice(0, i);
      const chunkB = openFence.slice(i) + suffix;
      const p = new ArtifactStreamParser(); // default PERFORM (buildMode false)
      const mid = p.push(chunkA);
      expect(mid.state, `perform fence split at offset ${i}`).toBe("OUTSIDE");
      expect(mid.primaryCode, `perform fence split at offset ${i}`).toBe("");
      p.push(chunkB);
      const final = p.onStreamEnd();
      expect(final.state, `perform fence split at offset ${i}`).toBe("OUTSIDE");
      expect(final.sawAnyMarker, `perform fence split at offset ${i}`).toBe(false);
      expect(final.primaryCode, `perform fence split at offset ${i}`).toBe("");
    }
  });
});
