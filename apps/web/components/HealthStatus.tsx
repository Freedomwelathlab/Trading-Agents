"use client";

import { useEffect, useState } from "react";

type Health = {
  status?: string;
  trading_mode?: string;
  live_trading_enabled?: boolean;
};

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
    <section className="rounded border border-neutral-300 p-4 dark:border-neutral-700">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        Backend health
      </h2>
      {loading && <p className="text-sm text-neutral-500">Checking…</p>}
      {error && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-300">
          {error}
        </p>
      )}
      {health && !error && (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          <dt className="text-neutral-500">status</dt>
          <dd>{health.status ?? "unknown"}</dd>
          <dt className="text-neutral-500">trading_mode</dt>
          <dd>{health.trading_mode ?? "unknown"}</dd>
          <dt className="text-neutral-500">live_trading_enabled</dt>
          <dd>{String(health.live_trading_enabled ?? "unknown")}</dd>
        </dl>
      )}
    </section>
  );
}
