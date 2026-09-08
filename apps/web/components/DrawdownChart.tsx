"use client";

import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { EmptyNote } from "@/components/ui/primitives";

export type DrawdownPoint = { date: string; drawdownPct: number };

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
 * Drawdown is naturally "how far below peak" — a magnitude, always >= 0
 * (see `lib/backtestMetrics.computeDrawdownSeries`). This chart negates it
 * for display only, so the filled area visually dips *below* the zero
 * baseline as drawdown deepens, reading the way a "loss" shape normally
 * does. The axis is labelled unambiguously so the sign flip is never left
 * to be inferred: "Drawdown from peak (%)", with the negative numbers
 * meaning "this far below peak" — a value never means an actual gain.
 */
function toDisplayValue(drawdownPct: number): number {
  return -Math.abs(drawdownPct);
}

function formatPctTick(value: number): string {
  return `${value}%`;
}

/**
 * Single-series drawdown-from-peak chart (Phase 56). Uses `--neg`
 * (`app/globals.css`, D060) for the fill/stroke, matching this app's
 * existing "loss colour" convention (`PortfolioView`/`BacktestPanel`'s
 * `pnlTone`/`returnTone`).
 */
export function DrawdownChart({ points }: { points: DrawdownPoint[] }) {
  if (points.length === 0) {
    return <EmptyNote>No drawdown to chart.</EmptyNote>;
  }

  const data = points.map((p) => ({ date: p.date, drawdown: toDisplayValue(p.drawdownPct) }));

  return (
    <div className="rounded-md border border-line bg-well p-3" data-testid="drawdown-chart">
      <ResponsiveContainer width="100%" height={220}>
        <AreaChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 4 }}>
          <defs>
            <linearGradient id="drawdown-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--neg)" stopOpacity={0.05} />
              <stop offset="100%" stopColor="var(--neg)" stopOpacity={0.35} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="var(--grid)" strokeDasharray="3 4" vertical={false} />
          <XAxis
            dataKey="date"
            tickFormatter={formatDateTick}
            stroke="var(--ink-muted)"
            tick={{ fill: "var(--ink-muted)", fontSize: 11 }}
            tickLine={false}
          />
          <YAxis
            tickFormatter={formatPctTick}
            stroke="var(--ink-muted)"
            tick={{ fill: "var(--ink-muted)", fontSize: 11 }}
            tickLine={false}
            width={56}
            label={{
              value: "Drawdown from peak (%)",
              angle: -90,
              position: "insideLeft",
              fill: "var(--ink-faint)",
              fontSize: 11,
            }}
          />
          <Tooltip
            formatter={(value) => [
              typeof value === "number" ? `${value.toFixed(2)}%` : String(value ?? ""),
              "Drawdown",
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
          <Area
            type="monotone"
            dataKey="drawdown"
            name="Drawdown"
            stroke="var(--neg)"
            strokeWidth={2}
            fill="url(#drawdown-fill)"
            dot={false}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
