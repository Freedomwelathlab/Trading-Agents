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

/**
 * Colours the headline return from the sign of the figure the backend
 * actually returned. Anything that is not a finite non-zero number gets
 * no colour rather than a guessed one; the raw string is displayed
 * verbatim either way.
 */
function returnTone(raw: string | undefined): "pos" | "neg" | undefined {
  const n = Number(raw);
  if (raw == null || raw === "" || !Number.isFinite(n) || n === 0) return undefined;
  return n > 0 ? "pos" : "neg";
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

import {
  ChartFrame,
  WIDTH,
  HEIGHT,
  PAD_LEFT,
  PAD_RIGHT,
  PAD_TOP,
  PAD_BOTTOM,
} from "@/components/ui/ChartFrame";
import {
  Alert,
  EmptyNote,
  Field,
  KeyValue,
  Panel,
  Stat,
  TableScroll,
  Term,
  Value,
  btnPrimary,
  hintClass,
  inputClass,
  monoInputClass,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";

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

  // Closes the same vertices down to the baseline for the area fill. No
  // point here is invented — every x/y comes from `buildBacktestPoints`.
  const area =
    points.length > 0
      ? `${path} ${points[points.length - 1].x.toFixed(2)},${HEIGHT - PAD_BOTTOM} ` +
        `${points[0].x.toFixed(2)},${HEIGHT - PAD_BOTTOM}`
      : "";

  return (
    <div className="flex flex-col gap-2">
      <div className="rounded-md border border-line bg-well p-3">
        <ChartFrame
          ariaLabel={`Simulated equity across ${curve.length} trading day${
            curve.length === 1 ? "" : "s"
          }`}
          min={min}
          max={max}
          testId="backtest-equity-line"
          linePoints={path}
          areaPoints={area}
        >
          {points.map((p) => (
            <circle
              key={p.point.date}
              cx={p.x}
              cy={p.y}
              r={3}
              className="fill-pos stroke-well"
              strokeWidth={1.5}
            >
              <title>{`${p.point.date} — equity ${p.point.equity}`}</title>
            </circle>
          ))}
        </ChartFrame>
      </div>
      <p className={hintClass}>
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
    <Panel
      title="Backtest"
      description={
        <>
          Runs the one hard-coded SMA(20)-crossover strategy against real historical
          daily closes, through the same Risk Engine and Portfolio Manager the live
          trade path uses, on a throwaway in-memory paper broker. It writes nothing
          and cannot touch a real broker&apos;s state. <code className="font-mono text-ink-muted">end_date</code>{" "}
          must be today (UTC) — the history provider only exposes the most recent
          closes as of now, so an arbitrary past window cannot be served honestly.
        </>
      }
    >
      <form
        onSubmit={handleSubmit}
        className="grid gap-3 md:grid-cols-2 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_auto] xl:items-end"
      >
        <Field label="Symbol">
          <input
            className={monoInputClass}
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            required
          />
        </Field>
        <Field label="Starting cash">
          <input
            className={monoInputClass}
            value={startingCash}
            onChange={(e) => setStartingCash(e.target.value)}
            required
          />
        </Field>
        <Field label="Start date">
          <input
            type="date"
            className={inputClass}
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
            required
          />
        </Field>
        <Field label="End date (today, UTC)">
          <input
            type="date"
            className={inputClass}
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
            required
          />
        </Field>

        <button type="submit" disabled={loading} className={btnPrimary}>
          {loading ? "Running…" : "Run backtest"}
        </button>
      </form>

      {errorDetail && (
        <Alert tone="error">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </Alert>
      )}

      {result && !errorDetail && (
        <div className="flex flex-col gap-5">
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            <Stat
              label="final_equity"
              value={result.final_equity}
              testId="bt-final-equity"
              tone="accent"
            />
            <Stat
              label="total_return_pct"
              value={result.total_return_pct}
              testId="bt-total-return"
              tone={returnTone(result.total_return_pct)}
            />
            <Stat
              label="max_drawdown_pct"
              value={result.max_drawdown_pct}
              testId="bt-max-drawdown"
            />
            <Stat
              label="num_trades"
              value={result.num_trades}
              testId="bt-num-trades"
              hint={
                result.num_trades === 0
                  ? "no completed round trips"
                  : `win_rate_pct ${result.win_rate_pct}`
              }
            />
          </div>

          <KeyValue>
            <Term>symbol</Term>
            <Value testId="bt-symbol">{result.symbol}</Value>
            <Term>starting_cash</Term>
            <Value testId="bt-starting-cash">{result.starting_cash}</Value>
            <Term>win_rate_pct</Term>
            <Value testId="bt-win-rate">
              {result.num_trades === 0 ? "n/a (no completed round trips)" : result.win_rate_pct}
            </Value>
          </KeyValue>

          <div className="rounded-md border border-line bg-well p-3">
            <h3 className="text-[11px] font-semibold uppercase tracking-[0.09em] text-ink-faint">
              Portfolio Manager interventions
            </h3>
            <KeyValue testId="bt-portfolio-counters" className="mt-2">
              <Term>portfolio_modified_trades</Term>
              <Value testId="bt-pm-modified">{result.portfolio_modified_trades}</Value>
              <Term>portfolio_modify_risk_blocked_trades</Term>
              <Value testId="bt-pm-modify-blocked">
                {result.portfolio_modify_risk_blocked_trades}
              </Value>
              <Term>portfolio_rejected_trades</Term>
              <Value testId="bt-pm-rejected">{result.portfolio_rejected_trades}</Value>
            </KeyValue>
            <p className={`mt-2 ${hintClass}`}>
              Counts Portfolio Manager decisions only. Risk Engine rejections are a
              separate gate and are deliberately not counted here, so the two stay
              distinguishable. A resized (MODIFY) quantity is always re-checked by the
              Risk Engine before it fills; the subset that the re-check then blocked is
              reported on its own line and never filled. All-zero counters mean this run
              recorded no Portfolio Manager intervention — not, on its own, that every
              trade was approved.
            </p>
          </div>

          {curve.length > 0 ? (
            <div className="flex flex-col gap-3">
              <EquityCurve curve={curve} />
              <TableScroll className="max-h-72 overflow-y-auto">
                <table className={tableClass}>
                  <caption className="sr-only">
                    Every plotted equity point, exactly as returned by the API
                  </caption>
                  <thead>
                    <tr className={theadRowClass}>
                      <th className={thClass}>date</th>
                      <th className={thClass}>equity</th>
                    </tr>
                  </thead>
                  <tbody>
                    {curve.map((p) => (
                      <tr key={p.date} className={tbodyRowClass}>
                        <td className={`${tdClass} text-ink-muted`}>{p.date}</td>
                        <td className={tdClass}>{p.equity}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableScroll>
            </div>
          ) : (
            <EmptyNote>
              The run returned an empty equity curve, so there is nothing to chart.
            </EmptyNote>
          )}
        </div>
      )}
    </Panel>
  );
}
