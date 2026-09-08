"use client";

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { EmptyNote } from "@/components/ui/primitives";

export type EquityCurvePoint = { date: string; equity: number };

export type EquityCurveSeries = { name: string; points: EquityCurvePoint[] };

/**
 * Colours cycled across series, in CSS custom properties (`app/globals.css`,
 * D060) so the chart tracks light/dark mode the same way the rest of the
 * dashboard does, rather than hard-coded hex values. `--pos` leads because
 * a single-run detail page (the only caller in this phase) plots one
 * series and that is this app's existing "equity/positive" colour
 * (`ChartFrame`/`PortfolioHistoryChart` use it for the same line). The
 * rest exist only for a future multi-series caller.
 */
const SERIES_COLORS = ["var(--pos)", "var(--accent)", "var(--neg)"] as const;

function formatCurrencyTick(value: number): string {
  return `$${Math.round(value).toLocaleString("en-US")}`;
}

function formatDateTick(dateStr: string): string {
  const parsed = new Date(`${dateStr}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return dateStr;
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(parsed);
}

/**
 * Merges one or more sparse series into a single Recharts dataset, aligned
 * by date. A date a given series has no point for is left absent on that
 * series' key for that row — Recharts breaks the line there (no
 * `connectNulls`) rather than this function inventing an interpolated
 * value between two real points.
 */
export function buildEquityChartData(
  series: EquityCurveSeries[],
): Record<string, string | number>[] {
  const dateSet = new Set<string>();
  for (const s of series) {
    for (const p of s.points) dateSet.add(p.date);
  }
  const dates = Array.from(dateSet).sort();

  return dates.map((date) => {
    const row: Record<string, string | number> = { date };
    for (const s of series) {
      const point = s.points.find((p) => p.date === date);
      if (point) row[s.name] = point.equity;
    }
    return row;
  });
}

/**
 * Equity curve for one or more named series (Phase 56/D0xx). A single-run
 * detail page passes exactly one series; the multi-series shape exists so
 * this could in principle back a future overlay/comparison view without a
 * prop-shape change, though no such caller is wired in this phase.
 *
 * Recharts is this app's first charting-library dependency — every prior
 * chart (`ChartFrame`/`PortfolioHistoryChart`/`BacktestPanel`'s equity
 * curve) is hand-rolled SVG and stays that way; this component and
 * `DrawdownChart` are the only two built on it.
 */
export function EquityCurveChart({ series }: { series: EquityCurveSeries[] }) {
  const hasData = series.some((s) => s.points.length > 0);
  if (!hasData) {
    return <EmptyNote>No equity curve to chart.</EmptyNote>;
  }

  const data = buildEquityChartData(series);

  return (
    <div
      className="rounded-md border border-line bg-well p-3"
      data-testid="equity-curve-chart"
    >
      <ResponsiveContainer width="100%" height={280}>
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 4 }}>
          <CartesianGrid stroke="var(--grid)" strokeDasharray="3 4" vertical={false} />
          <XAxis
            dataKey="date"
            tickFormatter={formatDateTick}
            stroke="var(--ink-muted)"
            tick={{ fill: "var(--ink-muted)", fontSize: 11 }}
            tickLine={false}
          />
          <YAxis
            tickFormatter={formatCurrencyTick}
            stroke="var(--ink-muted)"
            tick={{ fill: "var(--ink-muted)", fontSize: 11 }}
            tickLine={false}
            width={76}
          />
          <Tooltip
            formatter={(value, name) => [
              typeof value === "number" ? formatCurrencyTick(value) : String(value ?? ""),
              String(name ?? ""),
            ]}
            labelFormatter={(label) => formatDateTick(String(label ?? ""))}
            contentStyle={{
              background: "var(--surface)",
              border: "1px solid var(--line)",
              borderRadius: 6,
              color: "var(--ink)",
              fontSize: 12,
            }}
          />
          {series.length > 1 && (
            <Legend wrapperStyle={{ fontSize: 12, color: "var(--ink-muted)" }} />
          )}
          {series.map((s, i) => (
            <Line
              key={s.name}
              type="monotone"
              dataKey={s.name}
              name={s.name}
              stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
              strokeWidth={2}
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
