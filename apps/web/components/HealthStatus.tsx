"use client";

import { useEffect, useState } from "react";
import { Pill } from "@/components/ui/primitives";

type Health = {
  status?: string;
  trading_mode?: string;
  live_trading_enabled?: boolean;
};

/**
 * The backend's real `GET /health` payload, rendered as the dashboard's
 * top status strip (Phase 45 / D060 restyle — the fetch, the error path
 * and the fields shown are unchanged).
 *
 * The three values keep their backend field names as labels so what is on
 * screen stays traceable to the API. Colour is derived only from values
 * the backend actually sent: an absent field renders "unknown" in the
 * neutral tone, never a green "ok". `live_trading_enabled: true` is
 * called out in the danger tone because it is the single most
 * consequential fact on this page.
 */
export default function HealthStatus() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const res = await fetch("/api/health", { cache: "no-store" });
        const data = await res.json().catch(() => null);
        if (cancelled) return;
        if (!res.ok) {
          setError(data?.detail ?? `Health check failed (HTTP ${res.status})`);
          return;
        }
        setHealth(data);
      } catch {
        if (!cancelled) setError("Could not reach the trading API.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section
      aria-label="Backend health"
      className="flex flex-wrap items-center gap-x-2.5 gap-y-2 rounded-lg border border-line bg-surface px-3 py-2 shadow-[var(--shadow-panel)]"
    >
      <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-ink-faint">
        Backend health
      </span>
      <span className="h-4 w-px bg-line" aria-hidden="true" />

      {loading && <span className="text-sm text-ink-faint">Checking…</span>}

      {error && (
        <p role="alert" className="text-sm font-medium text-neg">
          {error}
        </p>
      )}

      {health && !error && (
        <>
          <Pill tone={health.status === "ok" ? "pos" : "neutral"}>
            <span className="text-ink-faint">status</span>
            <span>{health.status ?? "unknown"}</span>
          </Pill>
          <Pill tone="neutral">
            <span className="text-ink-faint">trading_mode</span>
            <span>{health.trading_mode ?? "unknown"}</span>
          </Pill>
          <Pill
            tone={
              health.live_trading_enabled === true
                ? "neg"
                : health.live_trading_enabled === false
                  ? "pos"
                  : "neutral"
            }
          >
            <span className="text-ink-faint">live_trading_enabled</span>
            <span>{String(health.live_trading_enabled ?? "unknown")}</span>
          </Pill>
        </>
      )}
    </section>
  );
}
