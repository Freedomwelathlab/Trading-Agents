import type { CSSProperties } from "react";
import { EmptyNote } from "@/components/ui/primitives";

export type MonthlyReturn = { year: number; month: number; returnPct: number };

const MONTH_LABELS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

/**
 * Background colour for one cell: `--pos`/`--neg` (`app/globals.css`,
 * D060) at an opacity scaled to `returnPct`'s magnitude relative to the
 * largest magnitude in the whole dataset, via `sqrt` so a handful of
 * outlier months don't flatten every other cell to near-transparent — a
 * simple, deliberately non-continuous ramp, not a perceptual colour model.
 * A zero return gets no tint at all, and a missing month (no entry in
 * `months`) never reaches this function — it renders as a dash instead.
 */
function cellStyle(returnPct: number, maxAbs: number): CSSProperties {
  if (returnPct === 0 || maxAbs === 0) return {};
  const magnitude = Math.sqrt(Math.abs(returnPct) / maxAbs);
  const opacity = 0.12 + magnitude * 0.68; // keeps the smallest real move visible, the largest strong but not opaque-black text-obscuring
  const varName = returnPct > 0 ? "--pos" : "--neg";
  return { backgroundColor: `color-mix(in srgb, var(${varName}) ${Math.round(opacity * 100)}%, transparent)` };
}

function toneClass(returnPct: number): string {
  if (returnPct > 0) return "text-pos";
  if (returnPct < 0) return "text-neg";
  return "text-ink-muted";
}

/**
 * Classic monthly-returns heatmap: years as rows, Jan-Dec as columns
 * (Phase 56). Plain CSS grid — a heatmap of coloured cells is not really a
 * "chart" in the axes-and-data-points sense Recharts is built for, so it
 * gets no charting-library dependency.
 */
export function MonthlyReturnsHeatmap({ months }: { months: MonthlyReturn[] }) {
  if (months.length === 0) {
    return <EmptyNote>No monthly returns to show.</EmptyNote>;
  }

  const byYearMonth = new Map<string, number>();
  let maxAbs = 0;
  for (const m of months) {
    byYearMonth.set(`${m.year}-${m.month}`, m.returnPct);
    maxAbs = Math.max(maxAbs, Math.abs(m.returnPct));
  }

  const years = Array.from(new Set(months.map((m) => m.year))).sort((a, b) => a - b);

  return (
    <div className="overflow-x-auto rounded-md border border-line bg-well p-3" data-testid="monthly-returns-heatmap">
      <table className="w-full min-w-max border-separate border-spacing-1 text-xs">
        <caption className="sr-only">Monthly returns by year, exactly as computed from the equity curve</caption>
        <thead>
          <tr>
            <th className="px-1.5 py-1 text-left text-[10px] font-semibold uppercase tracking-[0.09em] text-ink-faint">
              Year
            </th>
            {MONTH_LABELS.map((label) => (
              <th
                key={label}
                className="px-1.5 py-1 text-center text-[10px] font-semibold uppercase tracking-[0.09em] text-ink-faint"
              >
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {years.map((year) => (
            <tr key={year}>
              <td className="tnum px-1.5 py-1 font-mono font-semibold text-ink">{year}</td>
              {MONTH_LABELS.map((_, i) => {
                const month = i + 1;
                const key = `${year}-${month}`;
                const returnPct = byYearMonth.get(key);
                if (returnPct === undefined) {
                  return (
                    <td
                      key={key}
                      data-testid={`heatmap-cell-${year}-${month}`}
                      className="tnum rounded px-2 py-1.5 text-center font-mono text-ink-faint"
                    >
                      —
                    </td>
                  );
                }
                return (
                  <td
                    key={key}
                    data-testid={`heatmap-cell-${year}-${month}`}
                    style={cellStyle(returnPct, maxAbs)}
                    className={`tnum rounded px-2 py-1.5 text-center font-mono font-medium ${toneClass(returnPct)}`}
                    title={`${MONTH_LABELS[i]} ${year}: ${returnPct.toFixed(2)}%`}
                  >
                    {returnPct.toFixed(1)}%
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
