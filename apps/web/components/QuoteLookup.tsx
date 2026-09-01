"use client";

import { useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  KeyValue,
  Panel,
  Stat,
  Term,
  Value,
  btnPrimary,
  monoInputClass,
} from "@/components/ui/primitives";

type QuoteResponse = {
  symbol?: string;
  price?: string;
  as_of?: string;
  source?: string;
  detail?: string;
};

export default function QuoteLookup({ className }: { className?: string } = {}) {
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
    <Panel
      className={className}
      title="Quote lookup"
      description="A single live price straight from the configured market-data vendor. If none is wired the backend's own NOT_CONFIGURED sentinel is shown verbatim — never a stand-in price."
    >
      <form onSubmit={handleSubmit} className="flex flex-wrap gap-2">
        <input
          aria-label="Symbol"
          className={`${monoInputClass} min-w-[10rem] flex-1`}
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          placeholder="AAPL.US"
          required
        />
        <button type="submit" disabled={loading} className={btnPrimary}>
          {loading ? "Looking up…" : "Get quote"}
        </button>
      </form>

      {errorDetail && (
        <Alert tone="error">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </Alert>
      )}

      {result && !errorDetail && (
        <>
          <Stat label="price" value={result.price ?? "—"} tone="accent" />
          <KeyValue columns={1}>
            <Term>symbol</Term>
            <Value>{result.symbol}</Value>
            <Term>as_of</Term>
            <Value>{result.as_of}</Value>
            <Term>source</Term>
            <Value>{result.source}</Value>
          </KeyValue>
        </>
      )}
    </Panel>
  );
}
