"use client";

import { useState } from "react";
import Link from "next/link";
import {
  EmptyNote,
  Panel,
  Pill,
  TableScroll,
  btnGhost,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";
import type { BacktestRunSummary } from "@/components/RunBacktestForm";

function statusTone(status: string): "pos" | "neg" | "neutral" {
  if (status === "succeeded") return "pos";
  if (status === "failed") return "neg";
  return "neutral";
}

/**
 * Renders a metric that is `null` on any `"failed"` run as an em-dash,
 * never `0%` — the backend sends `null`, not zero, for a metric it never
 * computed (`BacktestRunSummary` in schemas_strategy_backtests.py).
 */
function pctText(raw: string | null): string {
  return raw == null ? "—" : raw;
}

/** Colours a percentage figure from the sign of the number the backend
 * actually returned. Mirrors `PortfolioView.pnlTone` / `BacktestPanel.returnTone`. */
function pctClass(raw: string | null): string {
  if (raw == null) return "";
  const n = Number(raw);
  if (!Number.isFinite(n) || n === 0) return "";
  return n > 0 ? "text-pos" : "text-neg";
}

/**
 * A version's persisted backtest run history (Phase 56), read-only.
 *
 * `runs` is supplied by the parent page, which owns the fetch — this
 * component never fetches on its own, matching `BacktestRunComparison`'s
 * split of "who fetches what" the phase plan lays out. It only renders the
 * table and reports checkbox selection upward via `onSelectionChange`, so
 * the parent can decide when to reveal the comparison view.
 */
export function BacktestRunList({
  strategyId,
  versionId,
  runs,
  onSelectionChange,
}: {
  strategyId: string;
  versionId: string;
  runs: BacktestRunSummary[];
  onSelectionChange: (selectedIds: string[]) => void;
}) {
  const [selected, setSelected] = useState<string[]>([]);

  function toggle(id: string) {
    setSelected((prev) => {
      const next = prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id];
      onSelectionChange(next);
      return next;
    });
  }

  return (
    <Panel
      title="Backtest runs"
      description="Every persisted run for this version, newest first, exactly as the backend returned it. Check two or more to compare them below."
    >
      {runs.length === 0 ? (
        <EmptyNote>No backtest runs yet for this version — run one above.</EmptyNote>
      ) : (
        <TableScroll>
          <table className={tableClass}>
            <caption className="sr-only">
              Backtest runs for version {versionId}, exactly as returned by the API
            </caption>
            <thead>
              <tr className={theadRowClass}>
                <th className={thClass}>
                  <span className="sr-only">Compare</span>
                </th>
                <th className={thClass}>Symbol</th>
                <th className={thClass}>Date range</th>
                <th className={thClass}>Status</th>
                <th className={thClass}>total_return_pct</th>
                <th className={thClass}>max_drawdown_pct</th>
                <th className={thClass}>win_rate_pct</th>
                <th className={thClass}>Action</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className={tbodyRowClass} data-testid="backtest-run-row">
                  <td className={tdClass}>
                    <input
                      type="checkbox"
                      aria-label={`Compare run ${r.id}`}
                      checked={selected.includes(r.id)}
                      onChange={() => toggle(r.id)}
                    />
                  </td>
                  <td className={`${tdClass} font-semibold`}>{r.symbol}</td>
                  <td className={`${tdClass} text-ink-muted`}>
                    {r.start_date} → {r.end_date}
                  </td>
                  <td className={tdClass}>
                    <Pill tone={statusTone(r.status)}>{r.status}</Pill>
                  </td>
                  <td
                    className={`${tdClass} ${pctClass(r.total_return_pct)}`}
                    data-testid={`return-${r.id}`}
                  >
                    {pctText(r.total_return_pct)}
                  </td>
                  <td className={tdClass} data-testid={`drawdown-${r.id}`}>
                    {pctText(r.max_drawdown_pct)}
                  </td>
                  <td className={tdClass} data-testid={`winrate-${r.id}`}>
                    {pctText(r.win_rate_pct)}
                  </td>
                  <td className={tdClass}>
                    <Link
                      className={btnGhost}
                      href={`/strategies/${strategyId}/backtests/${r.id}`}
                    >
                      View
                    </Link>
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

export default BacktestRunList;
