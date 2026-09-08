import {
  EmptyNote,
  Panel,
  TableScroll,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";

/** Matches the API's real trade shape verbatim — money/percent as strings. */
export type TradeRow = {
  side: string;
  entry_date: string;
  entry_price: string;
  exit_date: string;
  exit_price: string;
  quantity: string;
  return_pct: string;
};

/**
 * Colours `return_pct` from the sign of the number the backend actually
 * returned. Mirrors `PortfolioView.pnlTone` / `BacktestPanel.returnTone`
 * exactly — same convention, not a third one: anything that is not a
 * finite non-zero number gets no colour, and the raw string is still
 * displayed verbatim either way.
 */
function returnTone(raw: string | undefined): "pos" | "neg" | undefined {
  const n = Number(raw);
  if (raw == null || raw === "" || !Number.isFinite(n) || n === 0) return undefined;
  return n > 0 ? "pos" : "neg";
}

function returnClass(raw: string | undefined): string {
  const tone = returnTone(raw);
  return tone === "pos" ? "text-pos" : tone === "neg" ? "text-neg" : "";
}

/**
 * The completed round-trip ledger for one backtest run (Phase 56),
 * mirroring `TradeHistory.tsx`'s table styling exactly — `Panel` +
 * `TableScroll` + the same th/td classes — so it reads as the same kind
 * of table as the rest of the dashboard.
 */
export function BacktestTradeLedger({ trades }: { trades: TradeRow[] }) {
  return (
    <Panel title="Trades" description="Every completed round trip this run recorded, exactly as returned by the API.">
      {trades.length === 0 ? (
        <EmptyNote>This run completed no round trips, so there is nothing to list.</EmptyNote>
      ) : (
        <TableScroll>
          <table className={tableClass}>
            <caption className="sr-only">Every trade from this backtest run, exactly as returned by the API</caption>
            <thead>
              <tr className={theadRowClass}>
                <th className={thClass}>Side</th>
                <th className={thClass}>Entry date</th>
                <th className={thClass}>Entry price</th>
                <th className={thClass}>Exit date</th>
                <th className={thClass}>Exit price</th>
                <th className={thClass}>Quantity</th>
                <th className={thClass}>Return %</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t, i) => (
                <tr key={`${t.entry_date}-${t.exit_date}-${i}`} className={tbodyRowClass} data-testid="trade-row">
                  <td className={tdClass}>{t.side}</td>
                  <td className={`${tdClass} text-ink-muted`}>{t.entry_date}</td>
                  <td className={tdClass}>{t.entry_price}</td>
                  <td className={`${tdClass} text-ink-muted`}>{t.exit_date}</td>
                  <td className={tdClass}>{t.exit_price}</td>
                  <td className={tdClass}>{t.quantity}</td>
                  <td className={`${tdClass} ${returnClass(t.return_pct)}`} data-testid={`trade-return-${i}`}>
                    {t.return_pct}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}
    </Panel>
  );
}
