"use client";

import { useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import { subscribeToBrokerSelection } from "@/lib/brokerSelection";

type Position = {
  symbol: string;
  quantity: string;
  avg_cost: string;
  current_value: string;
  unrealized_pnl: string;
  realized_pnl: string;
};

type PortfolioSnapshot = {
  broker_id?: string;
  cash?: string;
  positions?: Position[];
  total_equity?: string;
  total_unrealized_pnl?: string;
  total_realized_pnl?: string;
  detail?: string;
};

/** Parses "SYM=price, SYM2=price2" into the marks object the API expects. */
function parseMarks(input: string): Record<string, string> {
  const marks: Record<string, string> = {};
  for (const pair of input.split(",")) {
    const [symbol, price] = pair.split("=").map((s) => s.trim());
    if (symbol && price) marks[symbol] = price;
  }
  return marks;
}

export default function PortfolioView() {
  const [brokerId, setBrokerId] = useState("");

  // D034: the broker discovery list can push a real, granted broker id
  // here so the user never has to paste a UUID. Pre-filling authorizes
  // nothing - the backend re-checks the grant on submit.
  useEffect(() => subscribeToBrokerSelection(setBrokerId), []);
  const [marksInput, setMarksInput] = useState("");

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<PortfolioSnapshot | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    const body = { marks: parseMarks(marksInput) };

    try {
      const res = await fetch(`/api/portfolio/${encodeURIComponent(brokerId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await res.json().catch(() => null)) as PortfolioSnapshot | null;
      setStatus(res.status);
      if (!res.ok) {
        // A 401 means the httpOnly session cookie is gone or dead;
        // redirect to login with a real reason rather than rendering
        // a bare HTTP 401 next to a form that can no longer work (D032).
        if (handleExpiredSession(res.status)) return;
        // Real backend sentinels/status codes only — 400 DATA_UNAVAILABLE:
        // (a held position's mark is missing), 403 (no VIEW_PORTFOLIO
        // permission or no broker grant), 404 (unknown broker_id) — never
        // a fabricated/placeholder portfolio for any of these.
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
        Portfolio
      </h2>
      <p className="mb-3 text-xs text-neutral-500">
        Real-time snapshot only — cash, positions, and totals as computed by
        the backend right now. For performance over time, see Portfolio
        history below, which charts persisted snapshots.
      </p>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Broker ID
          <input
            className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
            value={brokerId}
            onChange={(e) => setBrokerId(e.target.value)}
            placeholder="broker UUID"
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Marks for currently-held symbols (required if you hold positions)
          <input
            className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
            value={marksInput}
            onChange={(e) => setMarksInput(e.target.value)}
            placeholder="AAPL.US=150.25, TSLA.US=250"
          />
        </label>

        <button
          type="submit"
          disabled={loading}
          className="self-start rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
        >
          {loading ? "Loading…" : "View portfolio"}
        </button>
      </form>

      {errorDetail && (
        <p role="alert" className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </p>
      )}

      {result && !errorDetail && (
        <div className="mt-3 rounded bg-neutral-50 px-3 py-2 text-sm dark:bg-neutral-900">
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1">
            <dt className="opacity-70">cash</dt>
            <dd>{result.cash}</dd>
            <dt className="opacity-70">total_equity</dt>
            <dd>{result.total_equity}</dd>
            <dt className="opacity-70">total_unrealized_pnl</dt>
            <dd>{result.total_unrealized_pnl}</dd>
            <dt className="opacity-70">total_realized_pnl</dt>
            <dd>{result.total_realized_pnl}</dd>
          </dl>

          <h3 className="mt-3 mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">
            Positions
          </h3>
          {result.positions && result.positions.length > 0 ? (
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="text-xs uppercase tracking-wide text-neutral-500">
                  <th className="pr-3">Symbol</th>
                  <th className="pr-3">Quantity</th>
                  <th className="pr-3">Avg cost</th>
                  <th className="pr-3">Current value</th>
                  <th className="pr-3">Unrealized P&amp;L</th>
                  <th className="pr-3">Realized P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {result.positions.map((p) => (
                  <tr key={p.symbol}>
                    <td className="pr-3">{p.symbol}</td>
                    <td className="pr-3">{p.quantity}</td>
                    <td className="pr-3">{p.avg_cost}</td>
                    <td className="pr-3">{p.current_value}</td>
                    <td className="pr-3">{p.unrealized_pnl}</td>
                    <td className="pr-3">{p.realized_pnl}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-neutral-500">No open positions.</p>
          )}
        </div>
      )}
    </section>
  );
}
