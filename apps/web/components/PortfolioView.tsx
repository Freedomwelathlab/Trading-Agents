"use client";

import { useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import { subscribeToBrokerSelection } from "@/lib/brokerSelection";
import {
  Alert,
  EmptyNote,
  Field,
  Panel,
  Stat,
  TableScroll,
  btnPrimary,
  monoInputClass,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";

/**
 * Colours a P&L figure from the sign of the number the backend actually
 * returned. A value that is not a finite number (absent, or a string the
 * backend did not send as a decimal) gets no colour at all rather than a
 * guessed one, and the raw string is still displayed verbatim.
 */
function pnlTone(raw: string | undefined): "pos" | "neg" | undefined {
  const n = Number(raw);
  if (raw == null || raw === "" || !Number.isFinite(n) || n === 0) return undefined;
  return n > 0 ? "pos" : "neg";
}

function pnlClass(raw: string | undefined): string {
  const tone = pnlTone(raw);
  return tone === "pos" ? "text-pos" : tone === "neg" ? "text-neg" : "";
}

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

export default function PortfolioView({ className }: { className?: string } = {}) {
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
    <Panel
      className={className}
      title="Portfolio"
      description="Real-time snapshot only — cash, positions, and totals as computed by the backend right now. For performance over time, see Portfolio history, which charts persisted snapshots."
    >
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <Field label="Broker ID">
          <input
            className={monoInputClass}
            value={brokerId}
            onChange={(e) => setBrokerId(e.target.value)}
            placeholder="broker UUID"
            required
          />
        </Field>
        <Field
          label="Marks for currently-held symbols (required if you hold positions)"
          hint="Comma-separated SYMBOL=price pairs. A held position with no mark is a real 400 DATA_UNAVAILABLE from the backend, not a zero."
        >
          <input
            className={monoInputClass}
            value={marksInput}
            onChange={(e) => setMarksInput(e.target.value)}
            placeholder="AAPL.US=150.25, TSLA.US=250"
          />
        </Field>

        <button type="submit" disabled={loading} className={`${btnPrimary} self-start`}>
          {loading ? "Loading…" : "View portfolio"}
        </button>
      </form>

      {errorDetail && (
        <Alert tone="error">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </Alert>
      )}

      {result && !errorDetail && (
        <div className="flex flex-col gap-4">
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            <Stat label="total_equity" value={result.total_equity ?? "—"} tone="accent" />
            <Stat label="cash" value={result.cash ?? "—"} />
            <Stat
              label="total_unrealized_pnl"
              value={result.total_unrealized_pnl ?? "—"}
              tone={pnlTone(result.total_unrealized_pnl)}
            />
            <Stat
              label="total_realized_pnl"
              value={result.total_realized_pnl ?? "—"}
              tone={pnlTone(result.total_realized_pnl)}
            />
          </div>

          <div>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.09em] text-ink-faint">
              Positions
            </h3>
            {result.positions && result.positions.length > 0 ? (
              <TableScroll>
                <table className={tableClass}>
                  <thead>
                    <tr className={theadRowClass}>
                      <th className={thClass}>Symbol</th>
                      <th className={thClass}>Quantity</th>
                      <th className={thClass}>Avg cost</th>
                      <th className={thClass}>Current value</th>
                      <th className={thClass}>Unrealized P&amp;L</th>
                      <th className={thClass}>Realized P&amp;L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.positions.map((p) => (
                      <tr key={p.symbol} className={tbodyRowClass}>
                        <td className={`${tdClass} font-semibold`}>{p.symbol}</td>
                        <td className={tdClass}>{p.quantity}</td>
                        <td className={tdClass}>{p.avg_cost}</td>
                        <td className={tdClass}>{p.current_value}</td>
                        <td className={`${tdClass} ${pnlClass(p.unrealized_pnl)}`}>
                          {p.unrealized_pnl}
                        </td>
                        <td className={`${tdClass} ${pnlClass(p.realized_pnl)}`}>
                          {p.realized_pnl}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableScroll>
            ) : (
              <EmptyNote>No open positions.</EmptyNote>
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}
