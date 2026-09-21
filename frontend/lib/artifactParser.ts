// Pure, synchronous, side-effect-free streaming parser for TokenQuick
// artifacts (design §2, Requirements 1.3–1.5, 2.1–2.6, 9.1/9.3/9.4).
//
// This module is deliberately isolated from React and Sandpack and contains no
// timers: time-based fallback classification lives in the preview component
// (design §4), so the parser stays deterministic and unit-testable on fixed
// chunk sequences (Req 2.6).

// The artifact delimiter markers. Chosen so they never collide with Markdown
// code fences the model routinely emits (Req 1.2). The open marker carries a
// language/template tag: `<<<TOKENQUICK_ARTIFACT:react>>>`.
export const OPEN_PREFIX = "<<<TOKENQUICK_ARTIFACT:";
export const OPEN_SUFFIX = ">>>";
export const CLOSE_MARKER = "<<<END_ARTIFACT>>>";

// State machine states (Req 2.1). The primary artifact is the first opened
// block; once it closes we move to CLOSED and stay there — later complete
// blocks are captured as extras but never reopen the primary (Req 9.4).
export type ParserState = "OUTSIDE" | "IN_ARTIFACT" | "CLOSED";

// The single shared snapshot shape between the parser, page.tsx, and the
// preview component (design §2 / Data models).
export interface ParseResult {
  state: ParserState;
  lang: string | null; // parsed from the open marker (Req 1.4); empty/missing → null
  primaryCode: string; // partial while IN_ARTIFACT, final when CLOSED
  primaryClosed: boolean; // true once the primary close marker was seen (Req 2.5)
  extraBlocks: string[]; // additional CLOSED blocks after the primary (Req 9)
  sawAnyMarker: boolean; // false ⇒ treat generation as no-artifact (Req 1.5)
}

// ---------------------------------------------------------------------------
// SECONDARY fence-fallback markers (Option A: "custom marker preferred; fence
// fallback only when NO custom marker appears").
//
// The custom `<<<TOKENQUICK_ARTIFACT:lang>>>` path above stays the PRIMARY,
// authoritative path and is 100% unchanged — including its body-opacity
// guarantee that Markdown ``` fences INSIDE a custom artifact are literal
// content, never delimiters (Property 3 / body-opacity tests). The fence
// fallback below is strictly additive and is SUPPRESSED for the entire stream
// the moment a custom OPEN marker is (or ever was) seen (gated on the internal
// `sawCustomMarker` flag).
//
// Fence semantics (fallback only, when no custom marker has appeared):
//   • OPEN: a line-start Markdown fence of the form ```lang where
//     lang ∈ {html, css, javascript, js} (case-insensitive), tolerating
//     optional trailing whitespace after the lang and requiring the fence
//     "line" to terminate with a newline. js normalizes to javascript.
//   • CLOSE: the FIRST line that is exactly three backticks (optionally with
//     surrounding whitespace). Per the user's decision, the first closing ```
//     closes the fenced artifact (nested triple-backtick content closes early —
//     the agreed tradeoff).
//   • primaryCode is the content strictly BETWEEN the opening fence line and
//     the closing fence line.
// ---------------------------------------------------------------------------
const FENCE = "```";
// Allowed opening-fence languages → normalized template lang.
const FENCE_LANGS: Record<string, string> = {
  html: "html",
  css: "css",
  javascript: "javascript",
  js: "javascript",
};
// Longest allowed language token (for straddle-tail arithmetic).
const MAX_FENCE_LANG_LEN = Math.max(
  ...Object.keys(FENCE_LANGS).map((l) => l.length)
);

// The longest marker string. A pending tail can never need to be longer than
// this to decide whether it is a (partial) marker.
const MAX_MARKER_LENGTH = Math.max(
  OPEN_PREFIX.length,
  OPEN_SUFFIX.length,
  CLOSE_MARKER.length
);

// Returns true when `text` is a non-empty proper prefix of `marker` — i.e. the
// tail could still grow into a full marker once more bytes arrive.
function isPartialPrefixOf(text: string, marker: string): boolean {
  return (
    text.length > 0 &&
    text.length < marker.length &&
    marker.startsWith(text)
  );
}

// Returns true when the suffix of `text` starting at `from` could be the
// beginning of `marker` (used to decide how much of a chunk is safe to consume
// vs. must be buffered as a potential straddling marker — Req 2.3).
function suffixCouldStartMarker(text: string, marker: string): boolean {
  // Check every non-empty suffix of `text` (shorter than the marker) to see if
  // it is a prefix of `marker`. Only the longest such suffix matters, but the
  // caller retains from the earliest ambiguous index, so we scan from the
  // longest candidate downward and report the earliest start.
  const maxLen = Math.min(text.length, marker.length - 1);
  for (let len = maxLen; len >= 1; len--) {
    if (marker.startsWith(text.slice(text.length - len))) {
      return true;
    }
  }
  return false;
}

// Index of the earliest position in `text` from which a suffix begins a partial
// (not full) prefix of `marker`, or -1 if none. Used to compute how many bytes
// are safe to flush as literal content.
function earliestPartialMarkerStart(text: string, marker: string): number {
  const maxLen = Math.min(text.length, marker.length - 1);
  let earliest = -1;
  for (let len = 1; len <= maxLen; len++) {
    if (marker.startsWith(text.slice(text.length - len))) {
      earliest = text.length - len;
    }
  }
  return earliest;
}

// ---------------------------------------------------------------------------
// Fence-detection helpers (pure). These operate on absolute positions within a
// buffer and only recognize fences at a LINE START (index 0 or right after a
// "\n"), mirroring Markdown's line-oriented fences.
// ---------------------------------------------------------------------------

// Result of matching an opening fence at some line-start index.
interface OpenFenceMatch {
  // Normalized template lang (html | css | javascript).
  lang: string;
  // Index just past the opening fence LINE's terminating newline — i.e. where
  // the fenced body begins.
  bodyStart: number;
}

// Try to match a COMPLETE opening fence beginning exactly at `lineStart`.
// Requires: ``` + allowed-lang (case-insensitive) + optional trailing
// horizontal whitespace + a newline. Returns null if not a complete match at
// this position (either not a fence, or not yet terminated by a newline).
function matchOpenFenceAt(
  text: string,
  lineStart: number
): OpenFenceMatch | null {
  if (!text.startsWith(FENCE, lineStart)) return null;
  let i = lineStart + FENCE.length;
  // Read the language token: letters only (our allowlist is alphabetic).
  const langStart = i;
  while (i < text.length && /[A-Za-z]/.test(text[i])) i++;
  const rawLang = text.slice(langStart, i).toLowerCase();
  if (!(rawLang in FENCE_LANGS)) return null;
  // Optional trailing horizontal whitespace (spaces / tabs) before newline.
  let j = i;
  while (j < text.length && (text[j] === " " || text[j] === "\t")) j++;
  // Require the fence line to be terminated by a newline within the buffer.
  if (j >= text.length || text[j] !== "\n") return null;
  return { lang: FENCE_LANGS[rawLang], bodyStart: j + 1 };
}

// Could the substring starting at `lineStart` still GROW into a valid opening
// fence once more bytes arrive? Used to decide how much of an OUTSIDE tail to
// retain (straddle buffering) instead of discarding as conversational text.
// True for partial fences like "`", "``", "```", "```h", "```html" (no newline
// yet), "```html " (trailing ws, no newline yet), and any consistent prefix.
function couldBecomeOpenFence(text: string, lineStart: number): boolean {
  const tail = text.slice(lineStart);
  if (tail.length === 0) return false;
  // Still typing the backtick run.
  if (tail.length < FENCE.length) return FENCE.startsWith(tail);
  if (!tail.startsWith(FENCE)) return false;
  // We have the full ``` — inspect what follows.
  let i = FENCE.length;
  // Language letters so far.
  const langStart = i;
  while (i < tail.length && /[A-Za-z]/.test(tail[i])) i++;
  const rawLang = tail.slice(langStart, i);
  if (i === tail.length) {
    // The buffer ends inside the language token. It's viable iff the token so
    // far is a prefix of some allowed language (or empty, which is a prefix of
    // all of them). Bounded by the longest allowed language.
    if (rawLang.length > MAX_FENCE_LANG_LEN) return false;
    const lower = rawLang.toLowerCase();
    return Object.keys(FENCE_LANGS).some((l) => l.startsWith(lower));
  }
  // We have a complete language token (letters then a non-letter). It must be
  // an EXACT allowed language for the fence to still be viable.
  if (!(rawLang.toLowerCase() in FENCE_LANGS)) return false;
  // After the language, only trailing horizontal whitespace is allowed before
  // the (not-yet-arrived) newline. Any other char makes it a non-fence.
  for (let k = i; k < tail.length; k++) {
    const c = tail[k];
    if (c === "\n") return false; // a newline here would have matched already
    if (c !== " " && c !== "\t") return false;
  }
  return true; // trailing ws, waiting for the newline
}

// Match a CLOSING fence on the line beginning at `lineStart`: a line that is
// exactly ``` optionally surrounded by horizontal whitespace, terminated by a
// newline OR by end-of-buffer when `atEnd` is true (stream end). Returns the
// index just past the close (past its newline, or text.length at eof), or -1.
function matchCloseFenceAt(
  text: string,
  lineStart: number,
  atEnd: boolean
): number {
  let i = lineStart;
  while (i < text.length && (text[i] === " " || text[i] === "\t")) i++;
  if (!text.startsWith(FENCE, i)) return -1;
  i += FENCE.length;
  while (i < text.length && (text[i] === " " || text[i] === "\t")) i++;
  if (i < text.length && text[i] === "\n") return i + 1;
  if (i >= text.length) return atEnd ? text.length : -1;
  return -1; // extra non-ws chars after ``` ⇒ not a bare closing fence line
}

// Could the line beginning at `lineStart` still grow into a bare closing fence?
// (leading ws + partial ``` + optional trailing ws, no newline yet). Used to
// retain a straddling closing fence instead of flushing it into primaryCode.
function couldBecomeCloseFence(text: string, lineStart: number): boolean {
  let i = lineStart;
  while (i < text.length && (text[i] === " " || text[i] === "\t")) i++;
  const rest = text.slice(i);
  if (rest.length === 0) return true; // just leading ws so far
  if (rest.length < FENCE.length) return FENCE.startsWith(rest);
  if (!rest.startsWith(FENCE)) return false;
  // Full ``` present; only trailing horizontal ws may follow before newline.
  for (let k = FENCE.length; k < rest.length; k++) {
    const c = rest[k];
    if (c === "\n") return false; // would have matched as a close already
    if (c !== " " && c !== "\t") return false;
  }
  return true;
}

export class ArtifactStreamParser {
  private state: ParserState = "OUTSIDE";
  private lang: string | null = null;
  private primaryCode = "";
  private primaryClosed = false;
  private extraBlocks: string[] = [];
  private sawAnyMarker = false;

  // Internal-only gate for the SECONDARY fence fallback. `sawAnyMarker` (the
  // public field) is true when EITHER path engages so the preview renders; but
  // the fence fallback is suppressed for the whole stream once a CUSTOM marker
  // has ever been seen. This flag tracks exactly that (custom-marker-only) so
  // the "custom wins" precedence is honored. ParseResult's public shape is
  // deliberately unchanged.
  private sawCustomMarker = false;

  // True while the CURRENT primary artifact was opened via the fence fallback
  // (so IN_ARTIFACT close-scanning uses the fence close rule, not the custom
  // CLOSE_MARKER). Only meaningful while state === "IN_ARTIFACT".
  private primaryViaFence = false;

  // Gate for the SECONDARY Markdown-fence fallback. The fence fallback may only
  // OPEN an artifact when the parser has been told it is in BUILD task-mode.
  // Default is `false` (fail-safe): if the consumer never calls setBuildMode,
  // no fence fallback fires and only the authoritative CUSTOM marker path can
  // open an artifact. This gate has NO effect on the custom-marker path, on
  // body opacity, on CLOSE handling, on extraBlocks, or on the IN_ARTIFACT
  // fence-CLOSE logic — it strictly governs whether a fence may OPEN.
  private buildMode = false;

  // Signal the parser's task-mode. Call with `true` for BUILD (fence fallback
  // enabled) and `false` for PERFORM / unknown (fence fallback disabled).
  // Idempotent and safe to call multiple times; last call wins. In practice
  // page.tsx sets this once per stream — from the `task_mode` annotation, which
  // the backend guarantees to emit BEFORE any text delta — so the parser is in
  // the correct mode before it ever sees a fence.
  setBuildMode(enabled: boolean): void {
    this.buildMode = enabled;
  }

  // Bytes we have received but cannot yet fully classify — either literal
  // content whose tail might be the start of a marker, or the partial body of
  // an unterminated open marker. Retained until the next push completes it
  // (Req 2.3).
  private pending = "";

  // While consuming an extra block (after the primary closed), we accumulate
  // its body here until its own CLOSE_MARKER arrives.
  private extraInProgress: string | null = null;

  // Feed one text delta; returns the post-push snapshot (Req 2.2). Never throws
  // (Req 2.4).
  push(chunk: string): ParseResult {
    if (chunk.length > 0) {
      this.pending += chunk;
      this.drain(false);
    }
    return this.snapshot();
  }

  // Returns the current snapshot without consuming more input.
  snapshot(): ParseResult {
    return {
      state: this.state,
      lang: this.lang,
      primaryCode: this.primaryCode,
      primaryClosed: this.primaryClosed,
      extraBlocks: [...this.extraBlocks],
      sawAnyMarker: this.sawAnyMarker,
    };
  }

  // Finalize the stream. Any bytes still pending are flushed as literal content
  // (Req 2.3 flush note): if we are still IN_ARTIFACT they belong to the
  // primary body; if mid extra-block they belong to that extra block. The state
  // is left as-is so the component can apply the stream-end-open fallback
  // (Req 7.4).
  onStreamEnd(): ParseResult {
    this.drain(true);
    if (this.pending.length > 0) {
      if (this.state === "IN_ARTIFACT") {
        this.primaryCode += this.pending;
      } else if (this.state === "CLOSED" && this.extraInProgress !== null) {
        this.extraInProgress += this.pending;
      }
      // OUTSIDE / CLOSED-with-no-open-extra: leftover is non-artifact text and
      // is discarded from the artifact view (Req 1.5).
      this.pending = "";
    }
    return this.snapshot();
  }

  // Core scanning loop. Consumes `pending` as far as it can fully classify.
  // When `flush` is true (stream end) we stop retaining ambiguous tails and let
  // onStreamEnd handle the remainder.
  private drain(flush: boolean): void {
    // Loop until no more progress can be made this pass.
    // Guard against pathological non-progress with a simple length check.
    for (;;) {
      const before = this.pending.length;

      if (this.state === "OUTSIDE") {
        if (!this.consumeOutside(flush)) break;
      } else if (this.state === "IN_ARTIFACT") {
        if (!this.consumeInArtifact(flush)) break;
      } else {
        // CLOSED — scan for additional complete blocks (Req 9.3).
        if (!this.consumeClosed(flush)) break;
      }

      if (this.pending.length === before && this.state !== "OUTSIDE") {
        // No forward progress and not in a re-scannable outside state → wait
        // for more input.
        break;
      }
      if (this.pending.length === 0) break;
    }
  }

  // OUTSIDE: look for an open marker. Everything before it is non-artifact text
  // (discarded from the artifact view). Returns true if it made progress and
  // the loop should continue.
  private consumeOutside(flush: boolean): boolean {
    const idx = this.pending.indexOf(OPEN_PREFIX);
    if (idx !== -1) {
      // A complete-or-locatable CUSTOM open prefix is present. The custom path
      // is authoritative: engage it (and permanently suppress the fence
      // fallback). If the terminating suffix has not yet arrived, buffer from
      // the prefix and wait — we do NOT let the fence fallback fire while a
      // custom open marker is mid-arrival.
      const suffixIdx = this.pending.indexOf(
        OPEN_SUFFIX,
        idx + OPEN_PREFIX.length
      );
      if (suffixIdx === -1) {
        // Open marker not yet terminated — buffer from the prefix and wait.
        this.pending = this.pending.slice(idx);
        return false;
      }

      const rawLang = this.pending.slice(idx + OPEN_PREFIX.length, suffixIdx);
      this.lang = rawLang.length > 0 ? rawLang : null; // Req 1.4
      this.sawAnyMarker = true; // Req 1.5
      this.sawCustomMarker = true; // custom path engaged ⇒ fence fallback OFF
      this.state = "IN_ARTIFACT";
      this.primaryViaFence = false;
      this.primaryCode = "";
      this.primaryClosed = false;
      // Consume through the open marker's suffix; the rest is body.
      this.pending = this.pending.slice(suffixIdx + OPEN_SUFFIX.length);
      return true;
    }

    // No full custom OPEN_PREFIX present. If a custom marker has EVER been
    // seen, the fence fallback stays suppressed forever — behave exactly as the
    // original OUTSIDE logic (discard conversational text, retain a possible
    // partial custom-prefix tail).
    if (this.sawCustomMarker) {
      if (flush) {
        this.pending = "";
        return false;
      }
      const keepFrom = earliestPartialMarkerStart(this.pending, OPEN_PREFIX);
      this.pending = keepFrom === -1 ? "" : this.pending.slice(keepFrom);
      return false;
    }

    // SECONDARY fence fallback: no custom marker has ever appeared, so scan for
    // an opening Markdown fence at a line start.
    if (this.tryOpenFence(flush)) return true;

    // No fence opened. We must retain a tail that could still become EITHER a
    // custom open prefix OR an opening fence (straddle across chunks); the rest
    // is conversational text discarded from the artifact view.
    if (flush) {
      this.pending = "";
      return false;
    }
    this.pending = this.pending.slice(this.outsideSafeRetainFrom());
    return false;
  }

  // Scan `pending` for a COMPLETE opening fence at any line start. On success,
  // open the primary via the fence path and consume through the opening fence
  // line. Returns true iff a fence opened.
  private tryOpenFence(_flush: boolean): boolean {
    // The fence fallback is gated on BUILD task-mode. In PERFORM / unknown mode
    // it is fully disabled: a ```html/css/js fence must NOT open an artifact and
    // is treated as ordinary conversational text (like any other prose).
    if (!this.buildMode) return false;
    for (let ls = 0; ls <= this.pending.length; ls = this.nextLineStart(ls)) {
      const m = matchOpenFenceAt(this.pending, ls);
      if (m) {
        this.lang = m.lang; // already normalized (js → javascript)
        this.sawAnyMarker = true; // preview should render (Req 1.5 semantics)
        this.state = "IN_ARTIFACT";
        this.primaryViaFence = true;
        this.primaryCode = "";
        this.primaryClosed = false;
        this.pending = this.pending.slice(m.bodyStart);
        return true;
      }
      if (ls >= this.pending.length) break;
    }
    return false;
  }

  // Index of the next line start strictly after `from`, or a value past the end
  // when there is no further newline (so the loop terminates).
  private nextLineStart(from: number): number {
    const nl = this.pending.indexOf("\n", from);
    return nl === -1 ? this.pending.length + 1 : nl + 1;
  }

  // While OUTSIDE with no custom marker seen, compute the earliest index we
  // must retain so we never drop a straddling custom OPEN_PREFIX or a
  // straddling opening fence that spans the chunk boundary.
  private outsideSafeRetainFrom(): number {
    let earliest = this.pending.length;

    // (a) Retain a possible partial custom OPEN_PREFIX tail.
    const custom = earliestPartialMarkerStart(this.pending, OPEN_PREFIX);
    if (custom !== -1) earliest = Math.min(earliest, custom);

    // (b) Retain the current (final) line if it could still grow into an
    //     opening fence. Fences are only recognized at a line start, so only
    //     the last line-start needs checking. This retention only matters when
    //     the fence fallback is armed (BUILD mode); in PERFORM / unknown mode a
    //     partial fence tail is ordinary prose and must be discarded like any
    //     other conversational text.
    if (this.buildMode) {
      const lastNl = this.pending.lastIndexOf("\n");
      const lastLineStart = lastNl === -1 ? 0 : lastNl + 1;
      if (couldBecomeOpenFence(this.pending, lastLineStart)) {
        earliest = Math.min(earliest, lastLineStart);
      }
    }

    return earliest;
  }

  // IN_ARTIFACT: only the exact CLOSE_MARKER ends the block (Req 1.3). Backticks
  // and near-miss strings are appended to primaryCode verbatim.
  private consumeInArtifact(flush: boolean): boolean {
    if (this.primaryViaFence) return this.consumeInFenceArtifact(flush);

    const idx = this.pending.indexOf(CLOSE_MARKER);
    if (idx !== -1) {
      this.primaryCode += this.pending.slice(0, idx);
      this.pending = this.pending.slice(idx + CLOSE_MARKER.length);
      this.primaryClosed = true; // Req 2.5
      this.state = "CLOSED"; // primary closed; stays CLOSED (Req 9.4)
      return true;
    }

    // No full close marker yet. Flush everything except a trailing substring
    // that could be the start of a CLOSE_MARKER (straddle buffering, Req 2.3).
    if (flush) {
      // Let onStreamEnd append the remainder as literal body.
      return false;
    }
    const keepFrom = earliestPartialMarkerStart(this.pending, CLOSE_MARKER);
    if (keepFrom === -1) {
      this.primaryCode += this.pending;
      this.pending = "";
    } else {
      this.primaryCode += this.pending.slice(0, keepFrom);
      this.pending = this.pending.slice(keepFrom);
    }
    return false;
  }

  // IN_ARTIFACT via the fence fallback: the FIRST bare closing fence line
  // (exactly ```, optional surrounding whitespace) closes the block. Content
  // strictly between the opening fence and the closing fence is primaryCode;
  // the single newline that terminates the last content line (immediately
  // before the closing fence line) is NOT part of primaryCode.
  private consumeInFenceArtifact(flush: boolean): boolean {
    // Scan each line start for a bare closing fence. At stream end (`flush`) a
    // final bare ``` line need NOT be newline-terminated to count as a close
    // (atEnd), so `...\n```<eof>` still closes; a block with no ``` line at all
    // stays IN_ARTIFACT (Req 7.4).
    for (let ls = 0; ls <= this.pending.length; ls = this.nextLineStart(ls)) {
      const end = matchCloseFenceAt(this.pending, ls, flush);
      if (end !== -1) {
        // Body is everything before this line, minus the terminating newline of
        // the preceding content line (pending[ls-1] === "\n" when ls > 0).
        const bodyEnd = ls > 0 ? ls - 1 : 0;
        this.primaryCode += this.pending.slice(0, bodyEnd);
        this.pending = this.pending.slice(end);
        this.primaryClosed = true;
        this.state = "CLOSED";
        this.primaryViaFence = false;
        return true;
      }
      if (ls >= this.pending.length) break;
    }

    // No complete closing fence yet.
    if (flush) {
      // onStreamEnd appends the remainder as literal partial body (Req 7.4).
      return false;
    }

    // Retain the final line if it could still become a closing fence
    // (straddle); flush everything before it into primaryCode.
    const lastNl = this.pending.lastIndexOf("\n");
    const lastLineStart = lastNl === -1 ? 0 : lastNl + 1;
    if (couldBecomeCloseFence(this.pending, lastLineStart) && lastLineStart > 0) {
      // Keep from the newline that precedes the candidate close line: that
      // newline may either terminate the last content line (if the close
      // completes) or be ordinary body (if it does not), so it must not be
      // committed to primaryCode yet.
      const keepFrom = lastNl; // includes the "\n" at lastNl
      this.primaryCode += this.pending.slice(0, keepFrom);
      this.pending = this.pending.slice(keepFrom);
    } else if (couldBecomeCloseFence(this.pending, lastLineStart) && lastLineStart === 0) {
      // Entire buffer is a partial close-fence candidate at the very start —
      // retain all of it (no committed newline precedes it).
      // (primaryCode unchanged; pending retained.)
    } else {
      this.primaryCode += this.pending;
      this.pending = "";
    }
    return false;
  }

  // CLOSED: the primary is done. Scan for additional complete open→close blocks
  // and capture them into extraBlocks (Req 9.3); they never reopen the primary.
  private consumeClosed(flush: boolean): boolean {
    if (this.extraInProgress === null) {
      // Looking for the next open marker.
      const idx = this.pending.indexOf(OPEN_PREFIX);
      if (idx === -1) {
        if (flush) {
          this.pending = "";
          return false;
        }
        const keepFrom = earliestPartialMarkerStart(this.pending, OPEN_PREFIX);
        this.pending = keepFrom === -1 ? "" : this.pending.slice(keepFrom);
        return false;
      }
      const suffixIdx = this.pending.indexOf(
        OPEN_SUFFIX,
        idx + OPEN_PREFIX.length
      );
      if (suffixIdx === -1) {
        this.pending = this.pending.slice(idx);
        return false;
      }
      // Begin an extra block. We don't track its lang (extras are shown as
      // plain non-executed code — design §12 item 4).
      this.extraInProgress = "";
      this.pending = this.pending.slice(suffixIdx + OPEN_SUFFIX.length);
      return true;
    }

    // Accumulating an extra block until its own CLOSE_MARKER.
    const idx = this.pending.indexOf(CLOSE_MARKER);
    if (idx !== -1) {
      this.extraInProgress += this.pending.slice(0, idx);
      this.extraBlocks.push(this.extraInProgress);
      this.extraInProgress = null;
      this.pending = this.pending.slice(idx + CLOSE_MARKER.length);
      return true;
    }

    if (flush) {
      return false;
    }
    const keepFrom = earliestPartialMarkerStart(this.pending, CLOSE_MARKER);
    if (keepFrom === -1) {
      this.extraInProgress += this.pending;
      this.pending = "";
    } else {
      this.extraInProgress += this.pending.slice(0, keepFrom);
      this.pending = this.pending.slice(keepFrom);
    }
    return false;
  }
}

// Exported for potential reuse/testing; not load-bearing but keeps the marker
// arithmetic discoverable.
export const _internals = {
  MAX_MARKER_LENGTH,
  isPartialPrefixOf,
  suffixCouldStartMarker,
  earliestPartialMarkerStart,
  // Fence-fallback helpers (Option A). Exported for discoverability/testing.
  FENCE,
  FENCE_LANGS,
  matchOpenFenceAt,
  couldBecomeOpenFence,
  matchCloseFenceAt,
  couldBecomeCloseFence,
};
