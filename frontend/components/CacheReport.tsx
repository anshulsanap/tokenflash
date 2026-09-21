import { StatBadge } from "./StatBadge";

// Cache report data streamed from the backend `cache_report` annotation. These
// figures are cache-specific and cumulative for the session (Req 7.7) — kept
// separate from the compression-savings presentation (Req 7.4).
export interface CacheReportData {
  stageEnabled: boolean;
  hit: boolean;
  hits: number;
  misses: number;
  decisions: number;
  hitRate: number;
  tokensSavedFromCache: number;
  computeTimeSavedMs: number;
  tokensSavedThisHit: number;
  computeTimeSavedMsThisHit: number;
}

// Cache benchmark data from the `cache_benchmark` annotation. On a hit this
// carries the savings; on a miss the figures are zeroed (Req 10.6).
export interface CacheBenchmarkData {
  decision: "hit" | "miss";
  lookupLatencyMs: number;
  tokensSaved: number;
  inferenceTimeSavedMs: number;
}

// Pure presentational panel — no fetching, no hooks. Mirrors RedactionReport so
// the semantic cache reads as its own first-class pipeline stage, visually
// distinct (sky/cyan accent) from redaction (rose) and compression (emerald).
// Validates: Requirements 7.4, 7.7, 7.9, 8.5, 10.6
export function CacheReport({
  report,
  benchmark,
}: {
  report: CacheReportData | null;
  benchmark: CacheBenchmarkData | null;
}) {
  // Nothing has streamed yet for this session — render nothing so the pane
  // isn't cluttered before a generate run produces a report.
  if (!report && !benchmark) return null;

  const stageEnabled = report?.stageEnabled ?? true;
  const hitRate = report?.hitRate ?? 0;
  const decisions = report?.decisions ?? 0;

  return (
    <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-4 shadow-sm">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-sky-300 uppercase tracking-wide">
          Semantic Cache Report
        </h3>
        {stageEnabled ? (
          <span className="rounded-full bg-sky-500/20 px-2.5 py-0.5 text-xs font-mono font-bold text-sky-300">
            {(hitRate * 100).toFixed(0)}% hit rate
          </span>
        ) : (
          <span className="rounded-full bg-slate-600/40 px-2.5 py-0.5 text-xs font-medium text-slate-400">
            Stage off
          </span>
        )}
      </div>

      {/* Disabled state (Req 8.5): the stage was off/unavailable for this
          request; show a note and zeros rather than hiding the panel. */}
      {!stageEnabled && (
        <div className="rounded-lg border border-slate-700 bg-slate-900/60 p-3">
          <p className="text-xs text-slate-400">
            Semantic cache disabled — every request ran the full pipeline.
          </p>
          <div className="mt-3 flex gap-3 flex-wrap">
            <StatBadge label="Hit Rate" value="0%" />
            <StatBadge label="Tokens Saved from Cache" value={0} />
            <StatBadge label="Compute Time Saved" value="0 ms" />
          </div>
        </div>
      )}

      {/* Empty state (Req 7.9): stage on but nothing looked up this session. */}
      {stageEnabled && decisions === 0 && (
        <div className="rounded-lg border border-dashed border-slate-700 bg-slate-900/40 p-4 text-center">
          <p className="text-sm text-slate-500">
            No cache activity in this session yet.
          </p>
        </div>
      )}

      {/* Populated state (Req 7.7): cumulative session cache savings, shown as
          cache-specific StatBadge tiles separate from the compression report. */}
      {stageEnabled && decisions > 0 && report && (
        <div className="space-y-2">
          <div className="flex gap-3 flex-wrap">
            <StatBadge label="Hit Rate" value={`${(report.hitRate * 100).toFixed(0)}%`} />
            <StatBadge label="Tokens Saved from Cache" value={report.tokensSavedFromCache} />
            <StatBadge label="Compute Time Saved" value={`${report.computeTimeSavedMs} ms`} />
          </div>
          <p className="text-[11px] text-slate-500">
            {report.hits} hits · {report.misses} misses · {report.decisions} decisions
          </p>
        </div>
      )}

      {/* Benchmark sub-block (Req 10.6): same StatBadge layout/labels/units as
          the redaction benchmark. Savings on a hit, zeros on a miss. */}
      {benchmark && (
        <div className="space-y-2">
          <p className="text-xs text-slate-400 uppercase tracking-wider">
            Cache Benchmark
          </p>
          <div className="flex gap-3 flex-wrap">
            <StatBadge label="Lookup Latency" value={`${benchmark.lookupLatencyMs.toFixed(1)} ms`} />
            <StatBadge label="Decision" value={benchmark.decision} />
            <StatBadge label="Tokens Saved" value={benchmark.tokensSaved} />
            <StatBadge label="Inference Time Saved" value={`${benchmark.inferenceTimeSavedMs} ms`} />
          </div>
        </div>
      )}
    </div>
  );
}
