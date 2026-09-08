/**
 * Pure derived-metrics helpers for the single-run backtest detail page
 * (Phase 56). No React, no DOM — both functions take and return plain
 * numeric shapes so they are trivially unit-testable, and so the caller
 * (the page component) stays the only place that parses the API's string
 * decimals into numbers.
 */

export type EquityPoint = { date: string; equity: number };

export type DrawdownPoint = { date: string; drawdownPct: number };

/**
 * Running peak-to-trough decline at each point of a (possibly sparse)
 * equity curve, as a POSITIVE percentage: `(peak - equity) / peak * 100`,
 * where `peak` is the running maximum equity seen up to and including that
 * point — never the global maximum of the whole curve. A later new high
 * does not retroactively change an earlier point's drawdown.
 *
 * Empty input returns an empty array. A monotonically increasing curve
 * returns all zeros, since every point is itself the running peak.
 */
export function computeDrawdownSeries(equityCurve: EquityPoint[]): DrawdownPoint[] {
  let peak = -Infinity;
  const out: DrawdownPoint[] = [];
  for (const point of equityCurve) {
    if (point.equity > peak) peak = point.equity;
    // A non-positive running peak can only happen for a non-positive
    // starting equity, which is not a real trading scenario — guarded
    // here only so this never divides by zero.
    const drawdownPct = peak > 0 ? ((peak - point.equity) / peak) * 100 : 0;
    out.push({ date: point.date, drawdownPct });
  }
  return out;
}

export type MonthlyReturn = { year: number; month: number; returnPct: number };

/**
 * One entry per calendar month that has at least one equity point in the
 * curve. `returnPct` is the percent change from that month's FIRST plotted
 * equity point to its LAST — not from the previous month's last point.
 *
 * This is a deliberate simplification, not a bug: the equity curve is
 * sparse (one point per trading day the run actually produced a bar for),
 * so "first point in the curve this month" is the only boundary this
 * function can compute without assuming a specific trading calendar. A
 * month that has exactly one point in the curve therefore returns 0 —
 * first and last are the same point, so there is nothing to compare
 * within that month.
 *
 * Empty input returns an empty array. Result is sorted chronologically.
 */
export function computeMonthlyReturns(equityCurve: EquityPoint[]): MonthlyReturn[] {
  type Group = { year: number; month: number; first: number; last: number };
  const groups = new Map<string, Group>();

  for (const point of equityCurve) {
    const [yearStr, monthStr] = point.date.split("-");
    const year = Number(yearStr);
    const month = Number(monthStr); // 1-12, as it appears in the ISO date
    const key = `${year}-${monthStr}`;
    const existing = groups.get(key);
    if (existing) {
      existing.last = point.equity;
    } else {
      groups.set(key, { year, month, first: point.equity, last: point.equity });
    }
  }

  const result: MonthlyReturn[] = Array.from(groups.values()).map((g) => ({
    year: g.year,
    month: g.month,
    returnPct: g.first === 0 ? 0 : ((g.last - g.first) / g.first) * 100,
  }));

  result.sort((a, b) => (a.year !== b.year ? a.year - b.year : a.month - b.month));
  return result;
}
