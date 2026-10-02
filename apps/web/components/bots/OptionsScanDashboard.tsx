"use client";

import { useCallback, useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import { Alert, Panel, TableScroll, tableClass, tbodyRowClass, tdClass, thClass, theadRowClass } from "@/components/ui/primitives";
import ScanBoard from "./ScanBoard";
import type { OptionsBotScan } from "./types";

/**
 * The options bot's dashboard (Phase 107, D134): the underlying's scan,
 * the spread it points to, and that spread priced from the live delayed
 * chain - legs, credit or debit, max loss and how many the bot's capital
 * buys. Refreshes every minute.
 */
export default function OptionsScanDashboard({ botId, botName }: { botId: string; botName: string }) {
  const [scan, setScan] = useState<OptionsBotScan | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/api/options-bots/${botId}/scan`, { cache: "no-store" });
      if (handleExpiredSession(res.status)) return;
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      if (!res.ok) {
        setError((data?.detail as string | undefined) ?? `Scan failed (HTTP ${res.status})`);
        return;
      }
      setScan(data as unknown as OptionsBotScan);
      setError(null);
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    }
  }, [botId]);

  useEffect(() => {
    void Promise.resolve().then(load);
    const t = window.setInterval(() => void load(), 60_000);
    return () => window.clearInterval(t);
  }, [load]);

  const p = scan?.proposal ?? null;
  const net = p ? Number(p.net_price) : 0;
  return (
    <Panel
      title={`${botName} — dashboard`}
      description={
        scan?.mode === "signal"
          ? "Signal mode: the underlying's best qualifying setup picks the side - BUY sells a bull put spread below the market, SELL a bear call spread above it, WAIT opens nothing."
          : "Fixed structure: the bot opens this structure each session day; the scan shows what the underlying is doing meanwhile."
      }
    >
      {error ? <Alert>{error}</Alert> : null}
      {!scan && !error ? <p className="text-sm text-ink-faint">Scanning…</p> : null}
      {scan ? (
        <div className="flex flex-col gap-4">
          <div className="rounded-lg border border-line bg-surface p-4">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <span className="text-sm font-semibold text-ink">Proposed trade</span>
              <span className="font-mono text-xs text-ink-faint">{scan.structure_type ?? "none"} · {scan.mode}{scan.min_signal_score !== null ? ` · min score ${scan.min_signal_score}` : ""}</span>
            </div>
            <p className="text-sm text-ink">{scan.proposal_note}</p>
            {p ? (
              <div className="mt-3 flex flex-col gap-3">
                <div className="grid grid-cols-2 gap-x-6 gap-y-2 rounded-md bg-well p-3 text-xs sm:grid-cols-5">
                  <div><div className="text-ink-faint">expiry</div><div className="font-mono text-ink">{p.expiry}</div></div>
                  <div><div className="text-ink-faint">{net < 0 ? "credit" : "debit"} / share</div><div className="font-mono text-ink">{Math.abs(net).toFixed(2)}</div></div>
                  <div><div className="text-ink-faint">max loss / spread</div><div className="font-mono text-neg">{Number(p.max_loss_per_contract).toFixed(2)}</div></div>
                  <div><div className="text-ink-faint">max profit / spread</div><div className="font-mono text-pos">{p.max_profit_per_contract ? Number(p.max_profit_per_contract).toFixed(2) : "unbounded"}</div></div>
                  <div><div className="text-ink-faint">quantity · at risk</div><div className="font-mono text-ink">{p.quantity} · {Number(p.capital_at_risk).toFixed(2)}</div></div>
                </div>
                <TableScroll>
                  <table className={tableClass}>
                    <thead>
                      <tr className={theadRowClass}>
                        <th className={thClass}>side</th>
                        <th className={thClass}>contract</th>
                        <th className={thClass}>right</th>
                        <th className={thClass}>strike</th>
                        <th className={thClass}>bid</th>
                        <th className={thClass}>ask</th>
                        <th className={thClass}>modelled fill</th>
                        <th className={thClass}>delta</th>
                      </tr>
                    </thead>
                    <tbody>
                      {p.legs.map((l) => (
                        <tr key={l.contract_symbol} className={tbodyRowClass}>
                          <td className={tdClass}>{l.side}</td>
                          <td className={tdClass}>{l.contract_symbol}</td>
                          <td className={tdClass}>{l.right}</td>
                          <td className={tdClass}>{l.strike}</td>
                          <td className={tdClass}>{l.bid}</td>
                          <td className={tdClass}>{l.ask}</td>
                          <td className={tdClass}>{l.fill_price}</td>
                          <td className={tdClass}>{l.delta ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableScroll>
                <p className="text-xs text-ink-faint">Quotes: {p.quote_source}. Fills are modelled at the mid moved half the spread against you.</p>
              </div>
            ) : null}
          </div>
          <div className="rounded-md border-l-2 border-accent bg-well px-3 py-2 text-xs text-ink-muted">
            <span className="font-semibold text-ink">What the research measured: </span>
            {scan.evidence.join(" ")}
          </div>
          <ScanBoard board={scan.board} minScore={scan.min_signal_score ?? 5} />
        </div>
      ) : null}
    </Panel>
  );
}
