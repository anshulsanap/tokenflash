"use client";

import { useEffect, useState } from "react";

const BACKEND = "http://localhost:8000";

// Pull an error message out of a non-ok fetch Response. FastAPI error bodies
// use the shape {"detail": "..."} — surface that when present (Req 10.4),
// otherwise fall back to a generic message.
async function detailFrom(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string" && body.detail) return body.detail;
  } catch {
    /* body wasn't JSON — use fallback */
  }
  return fallback;
}

// Self-contained settings panel managing custom redaction terms + the stage
// toggle. Talks only to the local backend.
// Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 8.3, 8.4
export function RedactionSettings() {
  // Terms list. `null` means "not loaded" — distinct from an empty [] list so
  // a load failure never renders as an empty current list (Req 10.5).
  const [terms, setTerms] = useState<string[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [newTerm, setNewTerm] = useState("");
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [togglePending, setTogglePending] = useState(false);
  const [toggleError, setToggleError] = useState<string | null>(null);

  // Semantic cache toggle (Req 8.3/8.4) — a sibling switch reading and flipping
  // the local backend's /api/cache/toggle endpoint, independent of redaction.
  const [cacheEnabled, setCacheEnabled] = useState<boolean | null>(null);
  const [cacheTogglePending, setCacheTogglePending] = useState(false);
  const [cacheToggleError, setCacheToggleError] = useState<string | null>(null);

  // Power telemetry toggle (Req 7.1/7.2) — a sibling switch reading and
  // flipping the local backend's /api/power/toggle endpoint, independent of
  // redaction and cache.
  const [powerEnabled, setPowerEnabled] = useState<boolean | null>(null);
  const [powerTogglePending, setPowerTogglePending] = useState(false);
  const [powerToggleError, setPowerToggleError] = useState<string | null>(null);

  // Live preview / artifact toggle (Req 8.1/8.2) — a sibling switch reading and
  // flipping the local backend's /api/artifacts/toggle endpoint, independent of
  // redaction, cache, and power.
  const [artifactEnabled, setArtifactEnabled] = useState<boolean | null>(null);
  const [artifactTogglePending, setArtifactTogglePending] = useState(false);
  const [artifactToggleError, setArtifactToggleError] = useState<string | null>(null);

  // Initial load — GET terms and toggle state together (Req 10.1, 8.4).
  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      setLoading(true);
      setLoadError(null);
      try {
        const [termsRes, toggleRes, cacheToggleRes, powerToggleRes, artifactToggleRes] = await Promise.all([
          fetch(`${BACKEND}/api/redaction/terms`),
          fetch(`${BACKEND}/api/redaction/toggle`),
          fetch(`${BACKEND}/api/cache/toggle`),
          fetch(`${BACKEND}/api/power/toggle`),
          fetch(`${BACKEND}/api/artifacts/toggle`),
        ]);

        if (!termsRes.ok) {
          const msg = await detailFrom(termsRes, `Failed to load terms (HTTP ${termsRes.status})`);
          if (!cancelled) {
            setLoadError(msg);
            setTerms(null); // Req 10.5: never show a partial/empty list as current.
          }
          return;
        }

        const termsBody = await termsRes.json();
        if (!cancelled) setTerms(Array.isArray(termsBody.terms) ? termsBody.terms : []);

        // Toggle state is best-effort — a toggle read failure shouldn't blank
        // the terms list. Show a small toggle error instead.
        if (toggleRes.ok) {
          const toggleBody = await toggleRes.json();
          if (!cancelled) setEnabled(Boolean(toggleBody.enabled));
        } else if (!cancelled) {
          setToggleError(await detailFrom(toggleRes, "Could not read toggle state"));
        }

        // Cache toggle state is best-effort too — a failure shows a small
        // error under the cache switch, never blanking the terms list.
        if (cacheToggleRes.ok) {
          const cacheToggleBody = await cacheToggleRes.json();
          if (!cancelled) setCacheEnabled(Boolean(cacheToggleBody.enabled));
        } else if (!cancelled) {
          setCacheToggleError(await detailFrom(cacheToggleRes, "Could not read cache toggle state"));
        }

        // Power toggle state is best-effort too — a failure shows a small
        // error under the power switch, never blanking the terms list.
        if (powerToggleRes.ok) {
          const powerToggleBody = await powerToggleRes.json();
          if (!cancelled) setPowerEnabled(Boolean(powerToggleBody.enabled));
        } else if (!cancelled) {
          setPowerToggleError(await detailFrom(powerToggleRes, "Could not read power toggle state"));
        }

        // Artifact/live-preview toggle state is best-effort too — a failure
        // shows a small error under the switch, never blanking the terms list.
        if (artifactToggleRes.ok) {
          const artifactToggleBody = await artifactToggleRes.json();
          if (!cancelled) setArtifactEnabled(Boolean(artifactToggleBody.enabled));
        } else if (!cancelled) {
          setArtifactToggleError(await detailFrom(artifactToggleRes, "Could not read live preview toggle state"));
        }
      } catch {
        if (!cancelled) {
          setLoadError("Could not reach the backend to load redaction terms.");
          setTerms(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    load();
    return () => {
      cancelled = true;
    };
  }, []);

  // Add a term (Req 10.2). Retains the prior list on failure (Req 10.4).
  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    const term = newTerm.trim();
    if (!term || adding) return;

    setAdding(true);
    setActionError(null);
    try {
      const res = await fetch(`${BACKEND}/api/redaction/terms`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ term }),
      });
      if (!res.ok) {
        setActionError(await detailFrom(res, `Could not add term (HTTP ${res.status})`));
        return; // keep the previously displayed list unchanged
      }
      const body = await res.json();
      setTerms(Array.isArray(body.terms) ? body.terms : terms);
      setNewTerm(""); // clear only on success
    } catch {
      setActionError("Could not reach the backend to add the term.");
    } finally {
      setAdding(false);
    }
  };

  // Remove a term (Req 10.3). Retains the prior list on failure (Req 10.4).
  const handleRemove = async (term: string) => {
    if (removing) return;
    setRemoving(term);
    setActionError(null);
    try {
      const res = await fetch(`${BACKEND}/api/redaction/terms`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ term }),
      });
      if (!res.ok) {
        setActionError(await detailFrom(res, `Could not remove term (HTTP ${res.status})`));
        return;
      }
      const body = await res.json();
      setTerms(Array.isArray(body.terms) ? body.terms : terms);
    } catch {
      setActionError("Could not reach the backend to remove the term.");
    } finally {
      setRemoving(null);
    }
  };

  // Flip the stage toggle (Req 8.3/8.4). Reflects the returned {enabled}.
  const handleToggle = async () => {
    if (togglePending || enabled === null) return;
    setTogglePending(true);
    setToggleError(null);
    const next = !enabled;
    try {
      const res = await fetch(`${BACKEND}/api/redaction/toggle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next }),
      });
      if (!res.ok) {
        setToggleError(await detailFrom(res, `Could not update toggle (HTTP ${res.status})`));
        return;
      }
      const body = await res.json();
      setEnabled(Boolean(body.enabled));
    } catch {
      setToggleError("Could not reach the backend to update the toggle.");
    } finally {
      setTogglePending(false);
    }
  };

  // Flip the semantic cache toggle (Req 8.3/8.4). Reflects the returned
  // {enabled}. Mirrors handleToggle against /api/cache/toggle.
  const handleCacheToggle = async () => {
    if (cacheTogglePending || cacheEnabled === null) return;
    setCacheTogglePending(true);
    setCacheToggleError(null);
    const next = !cacheEnabled;
    try {
      const res = await fetch(`${BACKEND}/api/cache/toggle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next }),
      });
      if (!res.ok) {
        setCacheToggleError(await detailFrom(res, `Could not update cache toggle (HTTP ${res.status})`));
        return;
      }
      const body = await res.json();
      setCacheEnabled(Boolean(body.enabled));
    } catch {
      setCacheToggleError("Could not reach the backend to update the cache toggle.");
    } finally {
      setCacheTogglePending(false);
    }
  };

  // Flip the power telemetry toggle (Req 7.1/7.2). Reflects the returned
  // {enabled}. Mirrors handleCacheToggle against /api/power/toggle.
  const handlePowerToggle = async () => {
    if (powerTogglePending || powerEnabled === null) return;
    setPowerTogglePending(true);
    setPowerToggleError(null);
    const next = !powerEnabled;
    try {
      const res = await fetch(`${BACKEND}/api/power/toggle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next }),
      });
      if (!res.ok) {
        setPowerToggleError(await detailFrom(res, `Could not update power toggle (HTTP ${res.status})`));
        return;
      }
      const body = await res.json();
      setPowerEnabled(Boolean(body.enabled));
    } catch {
      setPowerToggleError("Could not reach the backend to update the power toggle.");
    } finally {
      setPowerTogglePending(false);
    }
  };

  // Flip the live preview / artifact toggle (Req 8.1/8.2). Reflects the
  // returned {enabled}. Mirrors handlePowerToggle against /api/artifacts/toggle.
  const handleArtifactToggle = async () => {
    if (artifactTogglePending || artifactEnabled === null) return;
    setArtifactTogglePending(true);
    setArtifactToggleError(null);
    const next = !artifactEnabled;
    try {
      const res = await fetch(`${BACKEND}/api/artifacts/toggle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next }),
      });
      if (!res.ok) {
        setArtifactToggleError(await detailFrom(res, `Could not update live preview toggle (HTTP ${res.status})`));
        return;
      }
      const body = await res.json();
      setArtifactEnabled(Boolean(body.enabled));
    } catch {
      setArtifactToggleError("Could not reach the backend to update the live preview toggle.");
    } finally {
      setArtifactTogglePending(false);
    }
  };

  return (
    <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-4 shadow-sm">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-rose-300 uppercase tracking-wide">
          Redaction Settings
        </h3>
      </div>

      {/* Stage toggle */}
      <div className="flex items-center justify-between rounded-lg bg-slate-900 px-3 py-2.5">
        <span className="text-xs text-slate-200">
          Redaction Stage:{" "}
          <span className={enabled ? "text-emerald-400 font-medium" : "text-slate-400 font-medium"}>
            {enabled === null ? "…" : enabled ? "On" : "Off"}
          </span>
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={enabled === true}
          aria-label="Toggle redaction stage"
          onClick={handleToggle}
          disabled={togglePending || enabled === null}
          className={`relative inline-flex h-6 w-11 items-center rounded-full transition
            disabled:opacity-50 disabled:cursor-not-allowed
            ${enabled ? "bg-emerald-500" : "bg-slate-600"}`}
        >
          <span
            className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition
              ${enabled ? "translate-x-6" : "translate-x-1"}`}
          />
        </button>
      </div>
      {toggleError && (
        <p className="text-[11px] text-red-300">{toggleError}</p>
      )}

      {/* Semantic cache toggle (Req 8.3/8.4) — reuses the redaction switch
          markup, independent state. */}
      <div className="flex items-center justify-between rounded-lg bg-slate-900 px-3 py-2.5">
        <span className="text-xs text-slate-200">
          Semantic Cache:{" "}
          <span className={cacheEnabled ? "text-emerald-400 font-medium" : "text-slate-400 font-medium"}>
            {cacheEnabled === null ? "…" : cacheEnabled ? "On" : "Off"}
          </span>
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={cacheEnabled === true}
          aria-label="Toggle semantic cache stage"
          onClick={handleCacheToggle}
          disabled={cacheTogglePending || cacheEnabled === null}
          className={`relative inline-flex h-6 w-11 items-center rounded-full transition
            disabled:opacity-50 disabled:cursor-not-allowed
            ${cacheEnabled ? "bg-emerald-500" : "bg-slate-600"}`}
        >
          <span
            className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition
              ${cacheEnabled ? "translate-x-6" : "translate-x-1"}`}
          />
        </button>
      </div>
      {cacheToggleError && (
        <p className="text-[11px] text-red-300">{cacheToggleError}</p>
      )}

      {/* Power telemetry toggle (Req 7.1/7.2) — reuses the switch markup,
          independent state. */}
      <div className="flex items-center justify-between rounded-lg bg-slate-900 px-3 py-2.5">
        <span className="text-xs text-slate-200">
          Power Telemetry:{" "}
          <span className={powerEnabled ? "text-emerald-400 font-medium" : "text-slate-400 font-medium"}>
            {powerEnabled === null ? "…" : powerEnabled ? "On" : "Off"}
          </span>
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={powerEnabled === true}
          aria-label="Toggle power telemetry stage"
          onClick={handlePowerToggle}
          disabled={powerTogglePending || powerEnabled === null}
          className={`relative inline-flex h-6 w-11 items-center rounded-full transition
            disabled:opacity-50 disabled:cursor-not-allowed
            ${powerEnabled ? "bg-emerald-500" : "bg-slate-600"}`}
        >
          <span
            className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition
              ${powerEnabled ? "translate-x-6" : "translate-x-1"}`}
          />
        </button>
      </div>
      {powerToggleError && (
        <p className="text-[11px] text-red-300">{powerToggleError}</p>
      )}

      {/* Live preview / artifact toggle (Req 8.1/8.2) — reuses the switch
          markup, independent state. */}
      <div className="flex items-center justify-between rounded-lg bg-slate-900 px-3 py-2.5">
        <span className="text-xs text-slate-200">
          Live Preview:{" "}
          <span className={artifactEnabled ? "text-emerald-400 font-medium" : "text-slate-400 font-medium"}>
            {artifactEnabled === null ? "…" : artifactEnabled ? "On" : "Off"}
          </span>
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={artifactEnabled === true}
          aria-label="Toggle live preview stage"
          onClick={handleArtifactToggle}
          disabled={artifactTogglePending || artifactEnabled === null}
          className={`relative inline-flex h-6 w-11 items-center rounded-full transition
            disabled:opacity-50 disabled:cursor-not-allowed
            ${artifactEnabled ? "bg-emerald-500" : "bg-slate-600"}`}
        >
          <span
            className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition
              ${artifactEnabled ? "translate-x-6" : "translate-x-1"}`}
          />
        </button>
      </div>
      {artifactToggleError && (
        <p className="text-[11px] text-red-300">{artifactToggleError}</p>
      )}

      {/* Add form */}
      <form onSubmit={handleAdd} className="flex gap-2">
        <input
          value={newTerm}
          onChange={(e) => setNewTerm(e.target.value)}
          placeholder="Add a custom term to redact"
          disabled={adding}
          className="flex-1 rounded-lg bg-slate-700 px-3 py-2 text-sm text-slate-100
                     placeholder-slate-500 outline-none ring-1 ring-slate-600
                     focus:ring-rose-500 disabled:opacity-50 transition"
        />
        <button
          type="submit"
          disabled={adding || !newTerm.trim()}
          className="rounded-lg bg-rose-600 px-4 py-2 text-sm font-medium text-white
                     hover:bg-rose-500 disabled:opacity-40 transition"
        >
          {adding ? "Adding…" : "Add"}
        </button>
      </form>

      {actionError && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2">
          <p className="text-[11px] text-red-300">{actionError}</p>
        </div>
      )}

      {/* Terms list / states */}
      <div className="space-y-1.5">
        <p className="text-xs text-slate-400 uppercase tracking-wider">Custom Terms</p>

        {loading && (
          <div className="flex items-center gap-2 rounded-lg bg-slate-900 px-3 py-2">
            <span className="h-2 w-2 rounded-full bg-rose-400 animate-pulse" />
            <span className="text-xs text-slate-400">Loading terms…</span>
          </div>
        )}

        {/* Load failure (Req 10.5): explicit error, no partial list shown. */}
        {!loading && loadError && (
          <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2">
            <p className="text-[11px] text-red-300">{loadError}</p>
          </div>
        )}

        {!loading && !loadError && terms && terms.length === 0 && (
          <div className="rounded-lg border border-dashed border-slate-700 bg-slate-900/40 px-3 py-2">
            <p className="text-xs text-slate-500">No custom terms yet.</p>
          </div>
        )}

        {!loading && !loadError && terms && terms.length > 0 && (
          <div className="space-y-1.5">
            {terms.map((term) => {
              const isRemoving = removing === term;
              return (
                <div
                  key={term}
                  className="flex items-center justify-between rounded-lg bg-slate-900 px-3 py-2"
                >
                  <span className="text-xs text-slate-200 truncate mr-2">{term}</span>
                  <button
                    type="button"
                    onClick={() => handleRemove(term)}
                    disabled={isRemoving || removing !== null}
                    aria-label={`Remove ${term}`}
                    className="rounded px-2 py-0.5 text-xs font-medium text-slate-400
                               hover:bg-slate-700 hover:text-red-300 shrink-0
                               disabled:opacity-40 disabled:cursor-not-allowed transition"
                  >
                    {isRemoving ? "Removing…" : "×"}
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
