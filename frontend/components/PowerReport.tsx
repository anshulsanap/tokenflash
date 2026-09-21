import { StatBadge } from "./StatBadge";

// Power report data streamed from the backend `power_report` annotation. These
// are the per-request hardware power / energy figures attributed to one
// generate request. Numeric fields are `number | null`: `null` means the figure
// was unavailable (empty window, source error, cache hit, or disabled stage)
// and is NEVER a fabricated 0 — a genuine measured 0 W stays distinguishable
// from the absence of a reading (Req 3.8, 5.4, 6.6).
export interface PowerReportData {
  stageEnabled: boolean;
  quality: "measured" | "estimated" | "unavailable";
  source: string;
  avgPowerWatts: number | null;
  energyJoules: number | null;
  cpuWatts: number | null;
  gpuWatts: number | null;
  packageWatts: number | null;
  sampleCount: number;
  durationSeconds: number;
}

// Render a numeric watt/joule figure, or "—" when the figure is unavailable
// (null). NEVER coerce null to 0 — the honesty invariant depends on the
// distinction (Req 6.6).
function fmt(value: number | null, unit: string): string {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(2)} ${unit}`;
}

// Pure presentational panel — no fetching, no hooks. Mirrors CacheReport so the
// power / energy stage reads as its own first-class pipeline stage, visually
// distinct (amber/yellow accent) from redaction (rose), cache (sky/cyan), and
// compression (emerald).
// Validates: Requirements 6.1, 6.2, 6.3, 6.5, 6.6, 7.5, 9.6
export function PowerReport({ report }: { report: PowerReportData | null }) {
  // Empty state (Req 6.3): nothing has streamed yet for this session.
  if (!report) {
    return (
      <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-4 shadow-sm">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-semibold text-amber-300 uppercase tracking-wide">
            Power / Energy Report
          </h3>
        </div>
        <div className="rounded-lg border border-dashed border-slate-700 bg-slate-900/40 p-4 text-center">
          <p className="text-sm text-slate-500">
            No power reading yet for this session.
          </p>
        </div>
      </div>
    );
  }

  // Quality badge (Req 6.2, 6.5, 6.6, 7.5): distinguish measured / estimated /
  // unavailable / stage off.
  const badge = !report.stageEnabled ? (
    <span className="rounded-full bg-slate-600/40 px-2.5 py-0.5 text-xs font-medium text-slate-400">
      Stage off
    </span>
  ) : report.quality === "measured" ? (
    <span className="rounded-full bg-amber-500/90 px-2.5 py-0.5 text-xs font-bold text-slate-900">
      measured
    </span>
  ) : report.quality === "estimated" ? (
    <span className="rounded-full border border-amber-400/70 px-2.5 py-0.5 text-xs font-medium text-amber-300">
      estimated
    </span>
  ) : (
    <span className="rounded-full bg-slate-600/40 px-2.5 py-0.5 text-xs font-medium text-slate-400">
      unavailable
    </span>
  );

  return (
    <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-4 shadow-sm">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-amber-300 uppercase tracking-wide">
          Power / Energy Report
        </h3>
        {badge}
      </div>

      {/* Disabled state (Req 7.5): the stage was off for this request. */}
      {!report.stageEnabled && (
        <div className="rounded-lg border border-slate-700 bg-slate-900/60 p-3">
          <p className="text-xs text-slate-400">
            Power telemetry disabled — no sampling ran for this request.
          </p>
        </div>
      )}

      {/* Unavailable state (Req 6.6): no fabricated wattage — figures render as
          "—" via the StatBadge tiles below, plus an explicit note. */}
      {report.stageEnabled && report.quality === "unavailable" && (
        <div className="rounded-lg border border-slate-700 bg-slate-900/60 p-3">
          <p className="text-xs text-slate-400">
            Power was unavailable for this request (no inference sampled).
          </p>
        </div>
      )}

      {/* Estimated labeling (Req 6.5): figures are captioned "estimated" so an
          estimate is never presented as a measurement. */}
      {report.stageEnabled && report.quality === "estimated" && (
        <p className="text-[11px] text-amber-300/80">
          Figures below are estimated from CPU utilization, not measured.
        </p>
      )}

      {/* Figures via the shared StatBadge (Req 6.1, 9.6): CPU / GPU / Package
          watts and Energy (J). A null field renders as "—", never 0 (Req 6.6). */}
      <div className="flex gap-3 flex-wrap">
        <StatBadge label="CPU" value={fmt(report.cpuWatts, "W")} />
        <StatBadge label="GPU" value={fmt(report.gpuWatts, "W")} />
        <StatBadge label="Package" value={fmt(report.packageWatts, "W")} />
        <StatBadge label="Energy" value={fmt(report.energyJoules, "J")} />
      </div>

      {/* Source identity + sample metadata (Req 6.1). */}
      <p className="text-[11px] text-slate-500">
        source: {report.source || "—"} · {report.sampleCount} samples ·{" "}
        {report.durationSeconds.toFixed(2)} s
      </p>
    </div>
  );
}
