// Small labeled figure used across the dashboard's right-pane report cards.
// Single source of truth so the compression, redaction, and router panels all
// render identical stat tiles (matching layout, labeling, and units).
export function StatBadge({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex flex-col items-center rounded-lg bg-slate-700 px-4 py-2">
      <span className="text-xs text-slate-400 uppercase tracking-wider">{label}</span>
      <span className="mt-1 text-lg font-mono font-bold text-indigo-400">{value}</span>
    </div>
  );
}
