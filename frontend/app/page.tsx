"use client";

import { Message } from "ai/react";
import { useEffect, useRef, useState } from "react";
import { StatBadge } from "../components/StatBadge";
import {
  RedactionReport,
  RedactionReportData,
  RedactionBenchmarkData,
} from "../components/RedactionReport";
import { RedactionSettings } from "../components/RedactionSettings";
import {
  CacheReport,
  CacheReportData,
  CacheBenchmarkData,
} from "../components/CacheReport";
import { PowerReport, PowerReportData } from "../components/PowerReport";
import { ArtifactPreview } from "../components/ArtifactPreview";
import { ArtifactStreamParser, ParseResult } from "../lib/artifactParser";

interface CompressionStats {
  originalTokens: number;
  compressedTokens: number;
  ratio: number;
  multiplier: number;
  compressedPrompt: string;
  // Set true when compression was skipped because the result was served from
  // the semantic cache (Req 9.6). All numeric fields are still present (zeros).
  skipped?: boolean;
}

interface DiffToken {
  text: string;
  kept: boolean;
  score: number;
}

interface CompressionDiff {
  original: string;
  tokens: DiffToken[];
  multiplier: number;
}

interface RealUsagePerSubtask {
  id: number;
  title: string;
  model: string;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
}

interface RealUsage {
  realInputTokens: number;
  realOutputTokens: number;
  realTotalTokens: number;
  realCostUnits: number;
  realInputCostUsd: number;
  realOutputCostUsd: number;
  realTotalCostUsd: number;
  perSubtask: RealUsagePerSubtask[];
}

interface SavingsBreakdown {
  provider?: string;
  baselineCostUsd: number;
  actualCostUsd: number;
  totalSavingUsd: number;
  savingsPct: number;
  compressionSavingUsd: number;
  routingSavingUsd: number;
  baselineModel: string;
}

interface SubtaskInfo {
  id: number;
  title: string;
  estimatedTokens: number;
  model: string;
  status: "pending" | "running" | "done" | "error";
}

interface RouterPlan {
  subtasks: SubtaskInfo[];
  totalEstimatedTokens: number;
  savingsVsSingleCall: number;
  savingsVsSingleCallPct: number;
}

type AppPhase = "elicit" | "generate" | "done";

function ChatBubble({ msg }: { msg: Message }) {
  const isUser = msg.role === "user";
  const display = msg.content.replace("REQUIREMENTS_COMPLETE", "").trim();
  if (!display) return null;
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} mb-3`}>
      <div className={`max-w-[82%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap shadow-sm
        ${isUser ? "bg-indigo-600 text-white rounded-br-sm" : "bg-slate-700 text-slate-100 rounded-bl-sm"}`}>
        {display}
      </div>
    </div>
  );
}

// Big "2.1×" style badge — the headline "physical token compression" figure
// that sets this apart from model-swapping routers.
function MultiplierBadge({ multiplier }: { multiplier: number }) {
  return (
    <div className="flex items-center gap-2 rounded-xl bg-gradient-to-r from-emerald-500/20 to-teal-500/20
                    border border-emerald-500/40 px-4 py-2">
      <span className="text-2xl font-mono font-black text-emerald-400">
        {multiplier.toFixed(1)}×
      </span>
      <span className="text-xs text-emerald-300 leading-tight">
        smaller<br />payload
      </span>
    </div>
  );
}

// Before/after diff: renders the ORIGINAL text with discarded tokens visibly
// struck through and greyed, so the user watches their messy language get
// physically compressed. This is the interactive, educational moment.
function CompressionDiffPanel({ diff }: { diff: CompressionDiff }) {
  return (
    <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-emerald-300 uppercase tracking-wide">
          Before → After Compression
        </h3>
        <MultiplierBadge multiplier={diff.multiplier} />
      </div>

      <div>
        <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">
          Original — dropped tokens struck through
        </p>
        <div className="rounded-lg bg-slate-900 p-3 text-xs leading-relaxed break-words">
          {diff.tokens.map((tok, i) => (
            <span
              key={i}
              title={`score ${tok.score}`}
              className={
                tok.kept
                  ? "text-green-300"
                  : "text-slate-600 line-through decoration-red-500/60"
              }
            >
              {tok.text}{" "}
            </span>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-4 text-[11px] text-slate-500">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-green-400" /> kept
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-red-500/60" /> dropped
        </span>
      </div>
    </div>
  );
}

// The 3-phase value story, visible at a glance. Names each phase as the
// differentiator it represents:
//   1. Pre-Flight Scoping  — actively scope requirements BEFORE any paid call
//   2. Token Compression   — physically shrink the payload (2×–5×)
//   3. Cost-Aware Routing  — send each subtask to the cheapest capable model
function PipelineIndicator({ phase }: { phase: AppPhase }) {
  const steps = [
    { key: "scoping", num: 1, label: "Pre-Flight Scoping", active: phase === "elicit", done: phase !== "elicit" },
    { key: "compress", num: 2, label: "Token Compression", active: phase === "generate", done: phase === "done" },
    { key: "route", num: 3, label: "Cost-Aware Routing", active: phase === "generate", done: phase === "done" },
  ];
  return (
    <div className="flex flex-wrap items-center gap-1 text-[10px]">
      {steps.map((s, i) => (
        <div key={s.key} className="flex items-center gap-1">
          <div
            className={`flex items-center gap-1 rounded-full px-2 py-0.5 font-medium transition
              ${s.active ? "bg-indigo-500/25 text-indigo-300 ring-1 ring-indigo-400/50" : ""}
              ${s.done ? "bg-green-500/15 text-green-400" : ""}
              ${!s.active && !s.done ? "bg-slate-700/50 text-slate-500" : ""}`}
          >
            <span className="font-mono">{s.done ? "✓" : s.num}</span>
            <span>{s.label}</span>
          </div>
          {i < steps.length - 1 && <span className="text-slate-600">→</span>}
        </div>
      ))}
    </div>
  );
}

// Format a small USD amount with enough precision to be meaningful for
// fractions of a cent (Bedrock costs are tiny per request).
function fmtUsd(v: number): string {
  if (v === 0) return "$0";
  if (v >= 0.01) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(6)}`;
}

// REAL Bedrock usage — the authoritative token counts AWS reports in its
// invocation metrics (the numbers you're billed on), shown distinctly from the
// heuristic compression estimate so the FinOps claim is credible, not guessed.
function RealUsagePanel({ usage }: { usage: RealUsage }) {
  const total = usage.realTotalCostUsd || 1;
  const inputPct = (usage.realInputCostUsd / total) * 100;
  const outputPct = (usage.realOutputCostUsd / total) * 100;
  return (
    <div className="rounded-xl bg-slate-800 border border-teal-600/40 p-4 space-y-3">
      <div className="flex items-center gap-2">
        <h3 className="text-xs font-semibold text-teal-300 uppercase tracking-wide">
          Real Local Token Usage
        </h3>
        <span className="rounded-full bg-teal-500/20 px-2 py-0.5 text-[10px] font-medium text-teal-300">
          measured on-device · $0 billed
        </span>
      </div>

      <div className="flex gap-3 flex-wrap">
        <StatBadge label="Real Input Tokens" value={usage.realInputTokens.toLocaleString()} />
        <StatBadge label="Real Output Tokens" value={usage.realOutputTokens.toLocaleString()} />
        <StatBadge label="Real Total" value={usage.realTotalTokens.toLocaleString()} />
      </div>

      {/* Cost — $0 locally; the split shows what dominates the token budget */}
      <div className="rounded-lg bg-slate-900 p-3 space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-[10px] uppercase tracking-wider text-slate-500">
            API cost (local)
          </span>
          <span className="font-mono text-sm font-bold text-teal-300">
            {usage.realTotalCostUsd > 0 ? fmtUsd(usage.realTotalCostUsd) : "$0.00"}
          </span>
        </div>
        {/* proportion bar — input vs output token share */}
        <div className="flex h-2 w-full overflow-hidden rounded-full bg-slate-700">
          <div className="bg-sky-500" style={{ width: `${inputPct}%` }} title="input share" />
          <div className="bg-amber-500" style={{ width: `${outputPct}%` }} title="output share" />
        </div>
        <div className="flex justify-between text-[11px]">
          <span className="text-sky-400">
            ● Input {fmtUsd(usage.realInputCostUsd)}{" "}
            <span className="text-slate-500">({inputPct.toFixed(1)}%)</span>
          </span>
          <span className="text-amber-400">
            Output {fmtUsd(usage.realOutputCostUsd)}{" "}
            <span className="text-slate-500">({outputPct.toFixed(1)}%)</span> ●
          </span>
        </div>
        <p className="text-[10px] text-slate-500 leading-relaxed">
          Running locally costs $0 in API fees. Token compression still matters:
          it shrinks the input the small local model must process, so it runs
          faster and fits more real work into a limited context window.
        </p>
      </div>

      {usage.perSubtask.length > 0 && (
        <div className="space-y-1">
          <p className="text-[10px] uppercase tracking-wider text-slate-500">
            Per subtask (input → output · cost)
          </p>
          {usage.perSubtask.map((s) => (
            <div key={s.id} className="flex items-center justify-between rounded-lg bg-slate-900 px-3 py-1.5">
              <span className="text-xs text-slate-200 truncate mr-2">{s.title}</span>
              <span className="font-mono text-[11px] text-teal-300 shrink-0">
                {s.inputTokens.toLocaleString()} → {s.outputTokens.toLocaleString()}
                <span className="text-slate-500"> · {fmtUsd(s.costUsd)}</span>
              </span>
            </div>
          ))}
        </div>
      )}

      <p className="text-[10px] text-slate-500 leading-relaxed">
        These are the exact input/output token counts the local model reported
        for every call in this run — measured on your machine, nothing billed.
        The compression report above is a heuristic estimate; this panel is
        ground truth.
      </p>
    </div>
  );
}

// The headline value story: honest combined savings vs the naive baseline
// (one uncompressed call to the premium model), attributed to the two real
// levers. This is what makes the pitch defensible — it shows routing does most
// of the work and compression adds the rest, rather than overclaiming.
function SavingsBreakdownPanel({ s }: { s: SavingsBreakdown }) {
  const local = s.provider !== "bedrock";
  const pct = s.totalSavingUsd > 0 ? s.totalSavingUsd : 1;
  const compPct = (s.compressionSavingUsd / pct) * 100;
  const routePct = (s.routingSavingUsd / pct) * 100;
  return (
    <div className="rounded-xl bg-gradient-to-br from-emerald-900/40 to-slate-800 border border-emerald-500/40 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-emerald-300 uppercase tracking-wide">
          {local ? "Cloud Cost Avoided" : "Cost Savings vs Naive Baseline"}
        </h3>
        <span className="font-mono text-2xl font-black text-emerald-400">
          {local ? "100%" : `${(s.savingsPct * 100).toFixed(0)}%`}
        </span>
      </div>

      <div className="flex items-center justify-between text-xs">
        <div className="flex flex-col">
          <span className="text-slate-500 text-[10px] uppercase">
            {local ? "Same work on cloud" : "Naive baseline"}
          </span>
          <span className="font-mono text-slate-400 line-through">{fmtUsd(s.baselineCostUsd)}</span>
          <span className="text-[10px] text-slate-500">
            {local ? `would've cost this on ${s.baselineModel}` : `1 uncompressed call · ${s.baselineModel}`}
          </span>
        </div>
        <span className="text-emerald-400 text-lg">→</span>
        <div className="flex flex-col items-end">
          <span className="text-slate-500 text-[10px] uppercase">
            {local ? "Ran locally" : "This pipeline"}
          </span>
          <span className="font-mono text-emerald-300 font-bold">
            {local ? "$0.00" : fmtUsd(s.actualCostUsd)}
          </span>
          <span className="text-[10px] text-emerald-500">
            {local ? "no API tokens billed" : `saved ${fmtUsd(s.totalSavingUsd)}`}
          </span>
        </div>
      </div>

      {/* Two-lever attribution */}
      <div className="space-y-1.5">
        <p className="text-[10px] uppercase tracking-wider text-slate-500">
          {local ? "What you avoided paying for" : "Where the savings come from"}
        </p>
        <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-slate-700">
          <div className="bg-purple-500" style={{ width: `${routePct}%` }} title="running locally" />
          <div className="bg-emerald-500" style={{ width: `${compPct}%` }} title="input compression" />
        </div>
        <div className="flex justify-between text-[11px]">
          <span className="text-purple-300">
            ● {local ? "Local compute" : "Model routing"} {fmtUsd(s.routingSavingUsd)}{" "}
            <span className="text-slate-500">({routePct.toFixed(0)}%)</span>
          </span>
          <span className="text-emerald-300">
            Input compression {fmtUsd(s.compressionSavingUsd)}{" "}
            <span className="text-slate-500">({compPct.toFixed(0)}%)</span> ●
          </span>
        </div>
      </div>

      <p className="text-[10px] text-slate-500 leading-relaxed">
        {local ? (
          <>
            You paid <span className="text-emerald-400 font-medium">$0</span> in API fees —
            everything ran on your own machine. The figure above is what the same
            work would have cost on {s.baselineModel} in the cloud. Token
            compression on top means the small local model does more with less.
          </>
        ) : (
          <>
            Baseline = one uncompressed call to {s.baselineModel} (the premium
            model). Routing to a cheaper model and compressing the input prompt
            cut the bill; routing usually dominates because output tokens carry
            most of the cost.
          </>
        )}
      </p>
    </div>
  );
}

export default function Home() {
  const [phase, setPhase] = useState<AppPhase>("elicit");
  const [canGenerate, setCanGenerate] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [stats, setStats] = useState<CompressionStats | null>(null);
  const [compressionDiff, setCompressionDiff] = useState<CompressionDiff | null>(null);
  const [realUsage, setRealUsage] = useState<RealUsage | null>(null);
  const [savings, setSavings] = useState<SavingsBreakdown | null>(null);
  const [routerPlan, setRouterPlan] = useState<RouterPlan | null>(null);
  const [routerStatus, setRouterStatus] = useState<string>("");
  const [subtaskStatuses, setSubtaskStatuses] = useState<Record<number, SubtaskInfo["status"]>>({});
  const [generatedCode, setGeneratedCode] = useState("");
  const [taskMode, setTaskMode] = useState<"build" | "perform" | null>(null);

  // Stable session id, generated ONCE per chat session. The backend rejects
  // generate requests without one and uses it to accumulate per-session
  // redaction counts. Regenerated only by resetChat().
  const [sessionId, setSessionId] = useState(() => crypto.randomUUID());
  const [redactionReport, setRedactionReport] = useState<RedactionReportData | null>(null);
  const [redactionBenchmark, setRedactionBenchmark] = useState<RedactionBenchmarkData | null>(null);
  const [redactionError, setRedactionError] = useState<string | null>(null);
  const [cacheReport, setCacheReport] = useState<CacheReportData | null>(null);
  const [cacheBenchmark, setCacheBenchmark] = useState<CacheBenchmarkData | null>(null);
  const [powerReport, setPowerReport] = useState<PowerReportData | null>(null);
  // Live artifact preview (private-on-device-artifacts stage). `artifactReport`
  // is the parser snapshot; `artifactStreamEnded` lets the component apply the
  // stream-end-while-open fallback (Req 7.4).
  const [artifactReport, setArtifactReport] = useState<ParseResult | null>(null);
  const [artifactStreamEnded, setArtifactStreamEnded] = useState(false);
  const [showSettings, setShowSettings] = useState(false);

  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Core streaming fetch — works for both elicit and generate phases
  const streamFromBackend = async (phase: AppPhase, msgs: Message[]) => {
    setIsLoading(true);

    // ONE parser per generation. Artifacts arrive as plain `0:` text and carry
    // no sessionId of their own, so the session guard here is structural: this
    // parser is local to this streamFromBackend invocation and only active for
    // phase === "generate", so a stale generation's parser is discarded when a
    // new generate starts (Req 6.4). Only the generate phase parses artifacts.
    const parser = new ArtifactStreamParser();
    if (phase === "generate") {
      // Clear the previous preview before the first token of a re-generate.
      setArtifactReport(null);
      setArtifactStreamEnded(false);
    }

    // Optimistically add a placeholder assistant message we'll fill in
    const assistantId = `a-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    setMessages((prev) => [
      ...prev,
      { id: assistantId, role: "assistant", content: "" },
    ]);

    try {
      const resp = await fetch("http://localhost:8000/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          phase,
          sessionId,
          messages: msgs.map((m) => ({ role: m.role, content: m.content })),
        }),
      });

      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let accumulated = "";
      let buffer = "";

      // Process ONE complete protocol line. Streamed chunks can split a line
      // in the middle, so we only ever call this with a full line (see the
      // buffering below) — otherwise a half-received JSON token would fail to
      // parse and the rendered text would look jumbled/duplicated.
      const processLine = (line: string) => {
        {
          // Text delta
          if (line.startsWith("0:")) {
            try {
              const token = JSON.parse(line.slice(2));
              accumulated += token;
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId ? { ...m, content: accumulated } : m
                )
              );
              // Feed the SAME decoded generate-phase text delta into the
              // artifact parser (a pure call). Only `0:` text — never `2:`
              // annotation frames, and never during the elicit phase.
              if (phase === "generate") setArtifactReport(parser.push(token));
            } catch { /* partial */ }
          }
          // Data annotation
          if (line.startsWith("2:")) {
            try {
              const payload = JSON.parse(line.slice(2))[0];
              if (payload?.event === "phase_complete") setCanGenerate(true);
              if (payload?.event === "compression_stats") {
                setStats({
                  originalTokens: payload.originalTokens,
                  compressedTokens: payload.compressedTokens,
                  ratio: payload.ratio,
                  multiplier: payload.multiplier ?? 1,
                  compressedPrompt: payload.compressedPrompt,
                  skipped: payload.skipped ?? false,
                });
              }
              if (payload?.event === "compression_diff") {
                setCompressionDiff({
                  original: payload.original,
                  tokens: payload.tokens,
                  multiplier: payload.multiplier ?? 1,
                });
              }
              if (payload?.event === "router_plan") {
                setRouterPlan({
                  subtasks: payload.subtasks,
                  totalEstimatedTokens: payload.totalEstimatedTokens,
                  savingsVsSingleCall: payload.savingsVsSingleCall,
                  savingsVsSingleCallPct: payload.savingsVsSingleCallPct ?? 0,
                });
              }
              if (payload?.event === "router_status") {
                setRouterStatus(payload.message);
              }
              if (payload?.event === "subtask_update") {
                setSubtaskStatuses((prev) => ({ ...prev, [payload.id]: payload.status }));
                setRouterPlan((prev) => {
                  if (!prev) return prev;
                  return {
                    ...prev,
                    subtasks: prev.subtasks.map((t) =>
                      t.id === payload.id ? { ...t, status: payload.status } : t
                    ),
                  };
                });
              }
              if (payload?.event === "real_usage") {
                setRealUsage({
                  realInputTokens: payload.realInputTokens,
                  realOutputTokens: payload.realOutputTokens,
                  realTotalTokens: payload.realTotalTokens,
                  realCostUnits: payload.realCostUnits,
                  realInputCostUsd: payload.realInputCostUsd ?? 0,
                  realOutputCostUsd: payload.realOutputCostUsd ?? 0,
                  realTotalCostUsd: payload.realTotalCostUsd ?? 0,
                  perSubtask: payload.perSubtask ?? [],
                });
              }
              if (payload?.event === "task_mode") {
                setTaskMode(payload.mode === "perform" ? "perform" : "build");
                // Gate the parser's fence fallback synchronously. The backend
                // emits this annotation BEFORE any `0:` text delta, and this
                // ordered loop processes it first, so the parser is in the
                // correct mode before it sees any fence. We cannot rely on the
                // async React `taskMode` state here — it won't be updated in
                // time within the same stream tick.
                parser.setBuildMode(payload.mode !== "perform");
              }
              if (payload?.event === "savings_breakdown") {
                setSavings({
                  provider: payload.provider,
                  baselineCostUsd: payload.baselineCostUsd,
                  actualCostUsd: payload.actualCostUsd,
                  totalSavingUsd: payload.totalSavingUsd,
                  savingsPct: payload.savingsPct,
                  compressionSavingUsd: payload.compressionSavingUsd,
                  routingSavingUsd: payload.routingSavingUsd,
                  baselineModel: payload.baselineModel,
                });
              }
              // Redaction report — only apply when the annotation's session id
              // matches the current session (Req 7.5: ignore mismatches).
              if (payload?.event === "redaction_report" && payload.sessionId === sessionId) {
                setRedactionReport({
                  stageEnabled: Boolean(payload.stageEnabled),
                  counts: payload.counts ?? {},
                  totalRedactions: payload.totalRedactions ?? 0,
                });
              }
              if (payload?.event === "redaction_benchmark" && payload.sessionId === sessionId) {
                setRedactionBenchmark({
                  stageEnabled: Boolean(payload.stageEnabled),
                  latencyMs: payload.latencyMs ?? 0,
                  charsRedacted: payload.charsRedacted ?? 0,
                  perCategoryCounts: payload.perCategoryCounts ?? {},
                });
              }
              // Redaction failed — surface a banner and clear the panels so no
              // stale/partial report is shown.
              if (payload?.event === "redaction_failure") {
                setRedactionError(`Redaction failed: ${payload.reason}`);
                setRedactionReport(null);
                setRedactionBenchmark(null);
              }
              // Cache report — session-guarded (Req 7.8): only apply when the
              // annotation's session id matches the current session.
              if (payload?.event === "cache_report" && payload.sessionId === sessionId) {
                setCacheReport({
                  stageEnabled: Boolean(payload.stageEnabled),
                  hit: Boolean(payload.hit),
                  hits: payload.hits ?? 0,
                  misses: payload.misses ?? 0,
                  decisions: payload.decisions ?? 0,
                  hitRate: payload.hitRate ?? 0,
                  tokensSavedFromCache: payload.tokensSavedFromCache ?? 0,
                  computeTimeSavedMs: payload.computeTimeSavedMs ?? 0,
                  tokensSavedThisHit: payload.tokensSavedThisHit ?? 0,
                  computeTimeSavedMsThisHit: payload.computeTimeSavedMsThisHit ?? 0,
                });
              }
              if (payload?.event === "cache_benchmark" && payload.sessionId === sessionId) {
                setCacheBenchmark({
                  decision: payload.decision === "hit" ? "hit" : "miss",
                  lookupLatencyMs: payload.lookupLatencyMs ?? 0,
                  tokensSaved: payload.tokensSaved ?? 0,
                  inferenceTimeSavedMs: payload.inferenceTimeSavedMs ?? 0,
                });
              }
              // Benchmark capture failed — clear the benchmark, keep the report
              // (Req 10.7). Session-guarded like the others.
              if (payload?.event === "cache_benchmark_unavailable" && payload.sessionId === sessionId) {
                setCacheBenchmark(null);
              }
              // Power report — session-guarded (Req 6.4): only apply when the
              // annotation's session id matches the current session. Use
              // `?? null` (NOT `?? 0`) so an unavailable numeric figure stays
              // null and never becomes a fabricated 0 (Req 6.6).
              if (payload?.event === "power_report" && payload.sessionId === sessionId) {
                setPowerReport({
                  stageEnabled: Boolean(payload.stageEnabled),
                  quality: payload.quality ?? "unavailable",
                  source: payload.source ?? "",
                  avgPowerWatts: payload.avgPowerWatts ?? null,
                  energyJoules: payload.energyJoules ?? null,
                  cpuWatts: payload.cpuWatts ?? null,
                  gpuWatts: payload.gpuWatts ?? null,
                  packageWatts: payload.packageWatts ?? null,
                  sampleCount: payload.sampleCount ?? 0,
                  durationSeconds: payload.durationSeconds ?? 0,
                });
              }
              // Power benchmark capture failed — clear the power report
              // (Req 6.4). Session-guarded like the others.
              if (payload?.event === "power_benchmark_unavailable" && payload.sessionId === sessionId) {
                setPowerReport(null);
              }
            } catch { /* partial */ }
          }
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        // Append the newly decoded text, then process only COMPLETE lines
        // (everything up to the last newline). Keep the trailing partial line
        // in `buffer` so it can be completed by the next chunk.
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";   // last element is the incomplete remainder
        for (const line of lines) {
          if (line) processLine(line);
        }
      }
      // Flush any final complete line left in the buffer.
      if (buffer.trim()) processLine(buffer);

      if (phase === "generate") {
        setGeneratedCode(accumulated);
        // Stream closed: finalize the parser so the component can apply the
        // stream-end-while-open fallback (Req 7.4).
        setArtifactReport(parser.onStreamEnd());
        setArtifactStreamEnded(true);
        setPhase("done");
      }
    } catch (err) {
      console.error(err);
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? { ...m, content: "⚠️ Error connecting to backend." }
            : m
        )
      );
    } finally {
      setIsLoading(false);
    }
  };

  const sendMessage = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isLoading) return;

    const userMsg: Message = {
      id: `u-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      role: "user",
      content: input.trim(),
    };

    const updatedMsgs = [...messages, userMsg];
    setMessages(updatedMsgs);
    setInput("");                    // clear AFTER we've captured the value

    await streamFromBackend("elicit", updatedMsgs);
  };

  const triggerGenerate = async () => {
    setPhase("generate");
    setGeneratedCode("");
    setTaskMode(null);
    setStats(null);
    setCompressionDiff(null);
    setRealUsage(null);
    setSavings(null);
    setRouterPlan(null);
    setRouterStatus("");
    setSubtaskStatuses({});
    setRedactionReport(null);
    setRedactionBenchmark(null);
    setRedactionError(null);
    setCacheReport(null);
    setCacheBenchmark(null);
    setPowerReport(null);
    setArtifactReport(null);
    setArtifactStreamEnded(false);
    await streamFromBackend("generate", messages);
  };

  // Start a fresh session without reloading the page — clears every panel and
  // returns to the empty elicitation screen.
  const resetChat = () => {
    setPhase("elicit");
    setCanGenerate(false);
    setMessages([]);
    setInput("");
    setIsLoading(false);
    setStats(null);
    setCompressionDiff(null);
    setRealUsage(null);
    setSavings(null);
    setRouterPlan(null);
    setRouterStatus("");
    setSubtaskStatuses({});
    setGeneratedCode("");
    setTaskMode(null);
    // Fresh session — new stable id and cleared redaction panels.
    setSessionId(crypto.randomUUID());
    setRedactionReport(null);
    setRedactionBenchmark(null);
    setRedactionError(null);
    setCacheReport(null);
    setCacheBenchmark(null);
    setPowerReport(null);
    setArtifactReport(null);
    setArtifactStreamEnded(false);
  };

  return (
    <div className="flex h-screen bg-slate-900 text-slate-100 font-sans overflow-hidden">

      {/* LEFT PANE */}
      <section className="flex w-1/2 flex-col border-r border-slate-700">
        <header className="border-b border-slate-700 bg-slate-800 px-5 py-3 shrink-0 space-y-2.5">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-sm font-bold text-indigo-400">⚡ TokenQuick</h1>
              <p className="text-xs text-slate-400">100% local · $0 API cost · token-optimized</p>
            </div>
            <div className="flex items-center gap-2">
              <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium
                ${phase === "elicit" ? "bg-amber-500/20 text-amber-400" : ""}
                ${phase === "generate" ? "bg-blue-500/20 text-blue-400" : ""}
                ${phase === "done" ? "bg-green-500/20 text-green-400" : ""}`}>
                {phase === "elicit" ? "Pre-Flight Scoping" : phase === "generate" ? "Compressing & Routing…" : "Complete"}
              </span>
              {messages.length > 0 && (
                <button
                  onClick={resetChat}
                  disabled={isLoading}
                  title="Start a new chat"
                  className="rounded-full border border-slate-600 px-2.5 py-0.5 text-xs font-medium
                             text-slate-300 hover:bg-slate-700 hover:text-white
                             disabled:opacity-40 disabled:cursor-not-allowed transition"
                >
                  ↺ New Chat
                </button>
              )}
            </div>
          </div>
          <PipelineIndicator phase={phase} />
        </header>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {messages.length === 0 && (
            <div className="mt-10 mx-auto max-w-sm text-center space-y-3">
              <p className="text-sm font-medium text-slate-300">
                ✈️ Pre-Flight Requirement Scoping
              </p>
              <p className="text-xs text-slate-500 leading-relaxed">
                Everything runs on a local model on your own machine — no cloud,
                no API bill. The chat first scopes exact requirements, then the
                prompt is compressed and routed across local models so a small
                model does more with less. Describe what you want to build to begin.
              </p>
            </div>
          )}
          {messages.map((m) => <ChatBubble key={m.id} msg={m} />)}
          <div ref={bottomRef} />
        </div>

        <div className="border-t border-slate-700 bg-slate-800 p-4 shrink-0 space-y-2">
          {phase === "elicit" && (
            <form onSubmit={sendMessage} className="flex gap-2">
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="What do you want to build?"
                disabled={isLoading}
                autoFocus
                className="flex-1 rounded-xl bg-slate-700 px-4 py-2 text-sm text-slate-100
                           placeholder-slate-500 outline-none ring-1 ring-slate-600
                           focus:ring-indigo-500 disabled:opacity-50 transition"
              />
              <button
                type="submit"
                disabled={isLoading || !input.trim()}
                className="rounded-xl bg-indigo-600 px-4 py-2 text-sm font-medium text-white
                           hover:bg-indigo-500 disabled:opacity-40 transition"
              >
                {isLoading ? "…" : "Send"}
              </button>
            </form>
          )}

          {canGenerate && phase === "elicit" && (
            <div className="space-y-1.5">
              <p className="text-center text-[11px] text-green-400">
                ✓ Requirements scoped — ready to build locally
              </p>
              <button
                onClick={triggerGenerate}
                disabled={isLoading}
                className="w-full rounded-xl bg-gradient-to-r from-indigo-600 to-purple-600
                           py-2.5 text-sm font-semibold text-white shadow-lg hover:opacity-90
                           disabled:opacity-40 transition"
              >
                ⚡ Compress &amp; Build Locally
              </button>
            </div>
          )}

          {phase !== "elicit" && (
            <p className="text-center text-xs text-slate-500">
              {phase === "generate" ? "Compressing → running on local models…" : "✅ Done. See right pane."}
            </p>
          )}
        </div>
      </section>

      {/* RIGHT PANE */}
      <section className="flex w-1/2 flex-col">
        <header className="border-b border-slate-700 bg-slate-800 px-5 py-3 shrink-0 flex items-center justify-between">
          <div>
            <h2 className="text-sm font-bold text-purple-400">Live Preview</h2>
            <p className="text-xs text-slate-400">Compression stats &amp; generated output</p>
          </div>
          <button
            onClick={() => setShowSettings((s) => !s)}
            title="Redaction settings"
            className={`rounded-full border px-2.5 py-0.5 text-xs font-medium transition
              ${showSettings
                ? "border-rose-500/50 bg-rose-500/15 text-rose-300"
                : "border-slate-600 text-slate-300 hover:bg-slate-700 hover:text-white"}`}
          >
            ⚙ Redaction Settings
          </button>
        </header>

        <div className="flex-1 overflow-y-auto p-5 space-y-5">
          {showSettings && <RedactionSettings />}

          {stats ? (
            <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-4">
              <div className="flex items-center justify-between">
                <h3 className="text-xs font-semibold text-purple-300 uppercase tracking-wide">
                  Token Compression Report
                </h3>
                {stats.skipped ? (
                  <span className="rounded-full bg-sky-500/20 px-2.5 py-0.5 text-xs font-medium text-sky-300">
                    skipped — served from cache
                  </span>
                ) : (
                  <span className="rounded-full bg-emerald-500/20 px-2.5 py-0.5 text-xs font-mono font-bold text-emerald-400">
                    {stats.multiplier.toFixed(1)}× smaller
                  </span>
                )}
              </div>
              <div className="flex gap-3 flex-wrap">
                <StatBadge label="Original Tokens" value={stats.originalTokens} />
                <StatBadge label="Compressed Tokens" value={stats.compressedTokens} />
                <StatBadge label="Savings" value={`${(stats.ratio * 100).toFixed(1)}%`} />
              </div>
              <div>
                <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">Compressed Prompt</p>
                <pre className="rounded-lg bg-slate-900 p-3 text-xs text-green-400 overflow-x-auto whitespace-pre-wrap break-all">
                  {stats.compressedPrompt}
                </pre>
              </div>
            </div>
          ) : (
            <div className="rounded-xl border border-dashed border-slate-700 bg-slate-800/50 p-8 text-center">
              <p className="text-slate-500 text-sm">
                Compression stats will appear here after you click{" "}
                <span className="text-indigo-400 font-medium">⚡ Compress &amp; Build Locally</span>.
              </p>
            </div>
          )}

          {/* Redaction — shown first (it runs before compression). Error
              banner sits above the report panel. */}
          {redactionError && (
            <div className="rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3">
              <p className="text-sm text-red-300">{redactionError}</p>
            </div>
          )}
          <RedactionReport report={redactionReport} benchmark={redactionBenchmark} />

          {/* Semantic cache — runs after redaction, before compression. Its
              savings are presented separately from the compression report. */}
          <CacheReport report={cacheReport} benchmark={cacheBenchmark} />

          {/* Power / Energy — the hardware power figures attributed to the
              generate request, rendered after the cache report. */}
          <PowerReport report={powerReport} />

          {/* Live artifact preview — the CLOSED primary artifact rendered in a
              network-restricted Sandpack sandbox, rendered after the power
              report. Passed through as-is; the component handles null/empty. */}
          <ArtifactPreview report={artifactReport} streamEnded={artifactStreamEnded} />

          {/* Before/After compression diff — the interactive differentiator */}
          {compressionDiff && compressionDiff.tokens.length > 0 && (
            <CompressionDiffPanel diff={compressionDiff} />
          )}

          {/* Task router progress */}
          {(routerStatus || routerPlan) && (
            <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-3">
              <h3 className="text-xs font-semibold text-amber-300 uppercase tracking-wide">
                Task Router
              </h3>
              {routerStatus && (
                <p className="text-xs text-slate-400 italic">{routerStatus}</p>
              )}
              {routerPlan && (
                <>
                  <div className="flex gap-3 flex-wrap">
                    <StatBadge label="Subtasks" value={routerPlan.subtasks.length} />
                    <StatBadge label="Est. Tokens" value={routerPlan.totalEstimatedTokens} />
                    <StatBadge
                      label="Cost Savings vs Single"
                      value={`${(routerPlan.savingsVsSingleCallPct * 100).toFixed(0)}%`}
                    />
                  </div>
                  <div className="space-y-1.5">
                    {routerPlan.subtasks.map((t) => (
                      <div key={t.id} className="flex items-center justify-between rounded-lg bg-slate-900 px-3 py-2">
                        <div className="flex items-center gap-2">
                          <span className={`h-2 w-2 rounded-full shrink-0
                            ${t.status === "done"    ? "bg-green-400" : ""}
                            ${t.status === "running" ? "bg-amber-400 animate-pulse" : ""}
                            ${t.status === "pending" ? "bg-slate-500" : ""}
                            ${t.status === "error"   ? "bg-red-400" : ""}`}
                          />
                          <span className="text-xs text-slate-200">{t.title}</span>
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="text-[10px] text-slate-500">{t.estimatedTokens} tok</span>
                          <span className="rounded px-1.5 py-0.5 text-[10px] bg-slate-700 text-indigo-300">{t.model}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>
          )}

          {/* Headline value story — honest combined savings vs naive baseline */}
          {savings && <SavingsBreakdownPanel s={savings} />}

          {/* Real Bedrock usage — ground-truth billed tokens */}
          {realUsage && <RealUsagePanel usage={realUsage} />}


          {((isLoading && phase === "generate") || generatedCode) && (
            <div className="rounded-xl bg-slate-800 border border-slate-700 p-4">
              <h3 className="text-xs font-semibold text-green-300 uppercase tracking-wide mb-3 flex items-center gap-2">
                {taskMode === "perform" ? "Result — Research / Notes" : "Generated Code"}
                {taskMode && (
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium normal-case
                    ${taskMode === "perform" ? "bg-sky-500/20 text-sky-300" : "bg-purple-500/20 text-purple-300"}`}>
                    {taskMode === "perform" ? "Performed task" : "Built tool"}
                  </span>
                )}
                {isLoading && phase === "generate" && (
                  <span className="h-1.5 w-1.5 rounded-full bg-green-400 animate-pulse" />
                )}
              </h3>
              <pre className="rounded-lg bg-slate-900 p-4 text-xs text-slate-200 overflow-x-auto whitespace-pre-wrap leading-relaxed">
                {generatedCode || "Streaming…"}
              </pre>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
