"use client";

import { useState } from "react";
import { handleExpiredSession } from "@/lib/session";

export type EquityPoint = {
  date: string;
  equity: string;
};

export type BacktestResult = {
  symbol: string;
  start_date: string;
  end_date: string;
  starting_cash: string;
  final_equity: string;
  total_return_pct: string;
  num_trades: number;
  win_rate_pct: string;
  max_drawdown_pct: string;
  equity_curve: EquityPoint[];
  portfolio_modified_trades: number;
  portfolio_modify_risk_blocked_trades: number;
  portfolio_rejected_trades: number;
};

/**
 * FastAPI returns a plain string `detail` for the hand-raised
 * `HTTPException`s (`NOT_CONFIGURED:`, `UNSUPPORTED_DATE_RANGE:`,
 * `DATA_UNAVAILABLE:`) but a list of objects for a 422 request-shape
 * validation error. Both are rendered as what they actually are — a 422
 * is never flattened into "[object Object]" or silently replaced with a
 * friendlier invented message.
 */
export function formatDetail(detail: unknown): string | null {
  if (detail == null) return null;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === "object") {
          const rec = item as Record<string, unknown>;
          const loc = Array.isArray(rec.loc) ? rec.loc.join(".") : undefined;
          const msg = typeof rec.msg === "string" ? rec.msg : JSON.stringify(item);
          return loc ? `${loc}: ${msg}` : msg;
        }
        return String(item);
      })
      .join("; ");
  }
  return JSON.stringify(detail);
}

/** Today in UTC, as `YYYY-MM-DD`. */
export function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

/** `days` calendar days before today (UTC), as `YYYY-MM-DD`. */
export function utcDaysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

const WIDTH = 640;
const HEIGHT = 200;
const PAD_LEFT = 8;
const PAD_RIGHT = 8;
const PAD_TOP = 12;
const PAD_BOTTOM = 12;

type Point = { x: number; y: number; point: EquityPoint; equity: number };

/**
 * Maps the backend's real `equity_curve` to SVG coordinates. Hand-rolled
 * for the same reason `PortfolioHistoryChart.tsx` is: a single-series
 * line does not justify a new charting dependency, and every vertex
 * plotted here is a value the backend returned. Nothing is interpolated,
 * smoothed, back-filled, or extended past the last real point.
 *
 * The x axis is by index, not by calendar date: the curve carries one
 * point per *trading* day (D025), so a calendar-scaled axis would imply
 * the run covered weekends and holidays it did not.
 */
export function buildBacktestPoints(curve: EquityPoint[]): {
  points: Point[];
  min: number;
  max: number;
} {
  const equities = curve.map((p) => Number(p.equity));
  const min = Math.min(...equities);
  const max = Math.max(...equities);
  const span = max - min;
  const plotW = WIDTH - PAD_LEFT - PAD_RIGHT;
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM;

  const points = curve.map((point, i) => {
    const equity = equities[i];
    const x =
      curve.length === 1 ? PAD_LEFT + plotW / 2 : PAD_LEFT + (i / (curve.length - 1)) * plotW;
    // A flat curve (span 0) is drawn as a centred horizontal line rather
    // than dividing by zero or inventing a slope — a backtest that never
    // traded really is flat.
    const ratio = span === 0 ? 0.5 : (equity - min) / span;
    const y = PAD_TOP + (1 - ratio) * plotH;
    return { x, y, point, equity };
  });

  return { points, min, max };
}

function EquityCurve({ curve }: { curve: EquityPoint[] }) {
  const { points, min, max } = buildBacktestPoints(curve);
  const path = points.map((p) => `${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" ");

  return (
    <div className="mt-3">
      <svg
        role="img"
        aria-label={`Simulated equity across ${curve.length} trading day${
          curve.length === 1 ? "" : "s"
        }`}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="w-full rounded border border-neutral-200 bg-neutral-50 dark:border-neutral-800 dark:bg-neutral-900"
      >
        <polyline
          data-testid="backtest-equity-line"
          points={path}
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          className="text-emerald-600 dark:text-emerald-400"
        />
        {points.map((p) => (
          <circle
            key={p.point.date}
            cx={p.x}
            cy={p.y}
            r={3}
            className="fill-emerald-600 dark:fill-emerald-400"
          >
            <title>{`${p.point.date} — equity ${p.point.equity}`}</title>
          </circle>
        ))}
      </svg>
      <p className="mt-1 text-xs text-neutral-500">
        {curve.length} trading day{curve.length === 1 ? "" : "s"} · equity range {min} – {max}
      </p>
    </div>
  );
}

/**
 * Runs the hard-coded SMA(20)-crossover backtest (D025) through
 * `POST /backtests` and renders the real `BacktestResult`.
 *
 * The Portfolio Manager counters (D035) are shown next to the Risk
 * Engine-gated results on purpose: before this panel existed there was no
 * way to see that the Portfolio Manager — not just the Risk Engine — had
 * intervened during a run. All three are always rendered, including when
 * they are 0, because "the Portfolio Manager changed nothing" and "the
 * Portfolio Manager was never consulted" are different facts and the
 * counters alone cannot distinguish them (D035); the note under the
 * counters says so rather than letting a row of zeroes imply approval.
 */
export default function BacktestPanel() {
  const [symbol, setSymbol] = useState("AAPL.US");
  const [startDate, setStartDate] = useState(utcDaysAgo(30));
  const [endDate, setEndDate] = useState(todayUtc());
  const [startingCash, setStartingCash] = useState("100000");

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    try {
      const res = await fetch("/api/backtests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol,
          start_date: startDate,
          end_date: endDate,
          starting_cash: startingCash,
        }),
      });
      const data = (await res.json().catch(() => null)) as
        | (Partial<BacktestResult> & { detail?: unknown })
        | null;
      setStatus(res.status);
      if (!res.ok) {
        // Real backend statuses only — 401 (session gone, handled by the
        // shared redirect), 400 `NOT_CONFIGURED:` (no market-data vendor
        // wired, D021/D015), 400 `UNSUPPORTED_DATE_RANGE:` (end_date is
        // not today UTC), 400/502 `DATA_UNAVAILABLE:` (too little history,
        // or the vendor itself failed), 422 (request-shape validation).
        // Never a placeholder curve or a zeroed result.
        if (handleExpiredSession(res.status)) return;
        setErrorDetail(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setResult(data as BacktestResult);
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  const curve = result?.equity_curve ?? [];

  return (
    <section className="rounded border border-neutral-300 p-4 dark:border-neutral-700">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        Backtest
      </h2>
      <p className="mb-3 text-xs text-neutral-500">
        Runs the one hard-coded SMA(20)-crossover strategy against real historical
        daily closes, through the same Risk Engine and Portfolio Manager the live
        trade path uses, on a throwaway in-memory paper broker. It writes nothing
        and cannot touch a real broker&apos;s state. <code>end_date</code> must be
        today (UTC) — the history provider only exposes the most recent closes as
        of now, so an arbitrary past window cannot be served honestly.
      </p>

      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Symbol
            <input
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              required
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Starting cash
            <input
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={startingCash}
              onChange={(e) => setStartingCash(e.target.value)}
              required
            />
          </label>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Start date
            <input
              type="date"
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              required
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            End date (today, UTC)
            <input
              type="date"
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
              required
            />
          </label>
        </div>

        <button
          type="submit"
          disabled={loading}
          className="self-start rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
        >
          {loading ? "Running…" : "Run backtest"}
        </button>
      </form>

      {errorDetail && (
        <p
          role="alert"
          className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300"
        >
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </p>
      )}

      {result && !errorDetail && (
        <div className="mt-3">
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-3">
            <dt className="opacity-70">symbol</dt>
            <dd data-testid="bt-symbol">{result.symbol}</dd>
            <dt className="opacity-70">starting_cash</dt>
            <dd data-testid="bt-starting-cash">{result.starting_cash}</dd>
            <dt className="opacity-70">final_equity</dt>
            <dd data-testid="bt-final-equity">{result.final_equity}</dd>
            <dt className="opacity-70">total_return_pct</dt>
            <dd data-testid="bt-total-return">{result.total_return_pct}</dd>
            <dt className="opacity-70">max_drawdown_pct</dt>
            <dd data-testid="bt-max-drawdown">{result.max_drawdown_pct}</dd>
            <dt className="opacity-70">num_trades</dt>
            <dd data-testid="bt-num-trades">{result.num_trades}</dd>
            <dt className="opacity-70">win_rate_pct</dt>
            <dd data-testid="bt-win-rate">
              {result.num_trades === 0 ? "n/a (no completed round trips)" : result.win_rate_pct}
            </dd>
          </dl>

          <h3 className="mt-4 text-xs font-semibold uppercase tracking-wide text-neutral-500">
            Portfolio Manager interventions
          </h3>
          <dl
            data-testid="bt-portfolio-counters"
            className="mt-1 grid grid-cols-2 gap-x-4 gap-y-1 text-sm"
          >
            <dt className="opacity-70">portfolio_modified_trades</dt>
            <dd data-testid="bt-pm-modified">{result.portfolio_modified_trades}</dd>
            <dt className="opacity-70">portfolio_modify_risk_blocked_trades</dt>
            <dd data-testid="bt-pm-modify-blocked">
              {result.portfolio_modify_risk_blocked_trades}
            </dd>
            <dt className="opacity-70">portfolio_rejected_trades</dt>
            <dd data-testid="bt-pm-rejected">{result.portfolio_rejected_trades}</dd>
          </dl>
          <p className="mt-1 text-xs text-neutral-500">
            Counts Portfolio Manager decisions only. Risk Engine rejections are a
            separate gate and are deliberately not counted here, so the two stay
            distinguishable. A resized (MODIFY) quantity is always re-checked by the
            Risk Engine before it fills; the subset that the re-check then blocked is
            reported on its own line and never filled. All-zero counters mean this run
            recorded no Portfolio Manager intervention — not, on its own, that every
            trade was approved.
          </p>

          {curve.length > 0 ? (
            <>
              <EquityCurve curve={curve} />
              <table className="mt-3 w-full text-left text-xs">
                <caption className="sr-only">
                  Every plotted equity point, exactly as returned by the API
                </caption>
                <thead>
                  <tr className="uppercase tracking-wide text-neutral-500">
                    <th className="pr-3">date</th>
                    <th className="pr-3">equity</th>
                  </tr>
                </thead>
                <tbody>
                  {curve.map((p) => (
                    <tr key={p.date}>
                      <td className="pr-3">{p.date}</td>
                      <td className="pr-3">{p.equity}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : (
            <p className="mt-3 text-sm text-neutral-500">
              The run returned an empty equity curve, so there is nothing to chart.
            </p>
          )}
        </div>
      )}
    </section>
  );
}
