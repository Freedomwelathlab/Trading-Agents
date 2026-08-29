"use client";

import { useState } from "react";
import { handleExpiredSession } from "@/lib/session";

type QuoteResponse = {
  symbol?: string;
  price?: string;
  as_of?: string;
  source?: string;
  detail?: string;
};

export default function QuoteLookup() {
  const [symbol, setSymbol] = useState("AAPL.US");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<QuoteResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);
    try {
      const res = await fetch(`/api/quote/${encodeURIComponent(symbol)}`, {
        cache: "no-store",
      });
      const data = (await res.json().catch(() => null)) as QuoteResponse | null;
      setStatus(res.status);
      if (!res.ok) {
        // A 401 means the httpOnly session cookie is gone or dead;
        // redirect to login with a real reason rather than rendering
        // a bare HTTP 401 next to a form that can no longer work (D032).
        if (handleExpiredSession(res.status)) return;
        // Render the real sentinel-prefixed detail string from the backend
        // (NOT_CONFIGURED: / NO_DATA_AVAILABLE:) — never a generic message.
        setErrorDetail(data?.detail ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setResult(data);
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="rounded border border-neutral-300 p-4 dark:border-neutral-700">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        Quote lookup
      </h2>
      <form onSubmit={handleSubmit} className="flex gap-2">
        <input
          className="flex-1 rounded border border-neutral-300 px-3 py-2 text-sm dark:border-neutral-700 dark:bg-neutral-900"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          placeholder="AAPL.US"
          required
        />
        <button
          type="submit"
          disabled={loading}
          className="rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
        >
          {loading ? "Looking up…" : "Get quote"}
        </button>
      </form>

      {errorDetail && (
        <p role="alert" className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </p>
      )}

      {result && !errorDetail && (
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          <dt className="text-neutral-500">symbol</dt>
          <dd>{result.symbol}</dd>
          <dt className="text-neutral-500">price</dt>
          <dd>{result.price}</dd>
          <dt className="text-neutral-500">as_of</dt>
          <dd>{result.as_of}</dd>
          <dt className="text-neutral-500">source</dt>
          <dd>{result.source}</dd>
        </dl>
      )}
    </section>
  );
}
