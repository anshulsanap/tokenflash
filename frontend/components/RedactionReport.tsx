import { StatBadge } from "./StatBadge";

// The redaction categories the backend can emit. Keeping this list here lets
// the benchmark grid default every built-in category to 0 (Req 9.2/9.6) and
// gives every category a stable, human-readable label.
export const REDACTION_CATEGORIES = [
  "ssn",
  "credit_card",
  "email",
  "phone",
  "api_key",
  "custom_term",
  "person",
] as const;

export type RedactionCategory = (typeof REDACTION_CATEGORIES)[number];

const CATEGORY_LABELS: Record<string, string> = {
  ssn: "SSN",
  credit_card: "Credit Card",
  email: "Email",
  phone: "Phone",
  api_key: "API Key",
  custom_term: "Custom Term",
  person: "Person/Entity",
};

function labelFor(category: string): string {
  return CATEGORY_LABELS[category] ?? category;
}

export interface RedactionReportData {
  stageEnabled: boolean;
  counts: Record<string, number>;
  totalRedactions: number;
}

export interface RedactionBenchmarkData {
  stageEnabled: boolean;
  latencyMs: number;
  charsRedacted: number;
  perCategoryCounts: Record<string, number>;
}

// Pure presentational panel — no fetching. Mirrors the Token Compression
// Report card so redaction reads as a first-class pipeline stage.
// Validates: Requirements 7.4, 7.6, 8.5, 9.5
export function RedactionReport({
  report,
  benchmark,
}: {
  report: RedactionReportData | null;
  benchmark: RedactionBenchmarkData | null;
}) {
  // Nothing has streamed yet for this session — render nothing so the pane
  // isn't cluttered before a generate run produces a report.
  if (!report && !benchmark) return null;

  const stageEnabled = report?.stageEnabled ?? benchmark?.stageEnabled ?? true;
  const total = report?.totalRedactions ?? 0;
  const counts = report?.counts ?? {};

  // Req 7.6: only categories with at least one redaction get a row.
  const nonzeroCategories = Object.entries(counts)
    .filter(([, count]) => count >= 1)
    .sort((a, b) => b[1] - a[1]);

  return (
    <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-4 shadow-sm">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-rose-300 uppercase tracking-wide">
          Redaction Report
        </h3>
        {stageEnabled ? (
          <span className="rounded-full bg-rose-500/20 px-2.5 py-0.5 text-xs font-mono font-bold text-rose-300">
            {total} redacted
          </span>
        ) : (
          <span className="rounded-full bg-slate-600/40 px-2.5 py-0.5 text-xs font-medium text-slate-400">
            Stage off
          </span>
        )}
      </div>

      {/* Disabled state (Req 8.5): the stage was off for this request; show a
          note and zeros rather than hiding the panel. */}
      {!stageEnabled && (
        <div className="rounded-lg border border-slate-700 bg-slate-900/60 p-3">
          <p className="text-xs text-slate-400">
            Redaction stage disabled — the summary was sent through unredacted.
            No sensitive data was scanned for this request.
          </p>
        </div>
      )}

      {/* Empty state (Req 7.6): stage on but nothing detected this session. */}
      {stageEnabled && total === 0 && (
        <div className="rounded-lg border border-dashed border-slate-700 bg-slate-900/40 p-4 text-center">
          <p className="text-sm text-slate-500">
            No sensitive data detected in this session.
          </p>
        </div>
      )}

      {/* Per-category counts (Req 7.4): one row per nonzero category. */}
      {stageEnabled && nonzeroCategories.length > 0 && (
        <div className="space-y-1.5">
          <p className="text-xs text-slate-400 uppercase tracking-wider">
            Redactions by category
          </p>
          {nonzeroCategories.map(([category, count]) => (
            <div
              key={category}
              className="flex items-center justify-between rounded-lg bg-slate-900 px-3 py-2"
            >
              <span className="text-xs text-slate-200">{labelFor(category)}</span>
              <span className="rounded px-2 py-0.5 text-xs font-mono font-bold bg-slate-700 text-rose-300">
                {count}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Benchmark figures (Req 9.5): same StatBadge layout/units as the
          compression stats panel. */}
      {benchmark && (
        <div className="space-y-2">
          <p className="text-xs text-slate-400 uppercase tracking-wider">
            Redaction Benchmark
          </p>
          <div className="flex gap-3 flex-wrap">
            <StatBadge label="Latency (ms)" value={benchmark.latencyMs} />
            <StatBadge label="Chars Redacted" value={benchmark.charsRedacted} />
            <StatBadge
              label="Total Redacted"
              value={Object.values(benchmark.perCategoryCounts).reduce(
                (sum, n) => sum + n,
                0
              )}
            />
          </div>

          {/* Per-category mini-grid: every built-in category, defaulting to 0
              (Req 9.2/9.6) so zeros are visible when nothing was redacted. */}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {REDACTION_CATEGORIES.map((category) => (
              <div
                key={category}
                className="flex items-center justify-between rounded-lg bg-slate-900 px-3 py-1.5"
              >
                <span className="text-[11px] text-slate-400 truncate mr-2">
                  {labelFor(category)}
                </span>
                <span className="font-mono text-[11px] font-bold text-slate-300 shrink-0">
                  {benchmark.perCategoryCounts[category] ?? 0}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
