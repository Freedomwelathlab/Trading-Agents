"use client";

import { useEffect, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  EmptyNote,
  Panel,
  TableScroll,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";
import type { BacktestRunSummary } from "@/components/RunBacktestForm";

/** `BacktestRunDetailResponse` — a summary plus the full equity curve and
 * trade list (schemas_strategy_backtests.py). This is the only component
 * in Phase 56's frontend half that needs the curve, so it is the only one
 * that fetches the detail endpoint rather than the list endpoint. */
export type BacktestRunDetail = BacktestRunSummary & {
  equity_curve: Array<{ date: string; equity: string }>;
  trades: Array<Record<string, unknown>>;
};

/** Mirrors `BacktestPanel.formatDetail` / `RunBacktestForm.formatDetail`. */
function formatDetail(detail: unknown): string | null {
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
 * A small, fixed rotation of this app's own theme-aware CSS custom
 * properties (`app/globals.css`) — never a hard-coded hex value, so a run's
 * line stays legible in both the light and dark palettes without this file
 * knowing either one's actual colours.
 *
 * None of these tokens was designed as a categorical palette (this app has
 * none yet, and adding one is out of this file's scope — `globals.css` is
 * not a file this phase touches). `--pos`/`--neg` are reused here as two
 * of the five slots purely for their visual contrast; unlike everywhere
 * else they colour a number in this app, a run's line colour here carries
 * no positive/negative verdict — it is chrome, distinguishing series only.
 */
const SERIES_COLORS = [
  "var(--accent)",
  "var(--neg)",
  "var(--ink)",
  "var(--pos)",
  "var(--ink-faint)",
];

function seriesColor(index: number): string {
  return SERIES_COLORS[index % SERIES_COLORS.length];
}

function runLabel(r: BacktestRunSummary): string {
  return `${r.symbol} ${r.start_date}→${r.end_date}`;
}

/**
 * Merges every *succeeded* run's equity curve into one Recharts-shaped
 * dataset keyed by date, one numeric column per run id. A `"failed"` run
 * is filtered out entirely here — it has no curve to contribute.
 *
 * A date one run has no point for is simply absent from that run's key on
 * that row (not `0`, not the previous value carried forward) — Recharts'
 * default `connectNulls={false}` then breaks that run's line at the gap
 * rather than interpolating or connecting across a date that is not real
 * data, matching this codebase's existing equity-chart rule
 * (`BacktestPanel.buildBacktestPoints`: "nothing is interpolated").
 */
export function buildComparisonSeries(
  runs: BacktestRunDetail[],
): Array<{ date: string } & Record<string, number>> {
  const succeeded = runs.filter((r) => r.status === "succeeded");
  const maps = succeeded.map(
    (r) => new Map(r.equity_curve.map((p) => [p.date, Number(p.equity)])),
  );
  const dateSet = new Set<string>();
  maps.forEach((m) => m.forEach((_v, k) => dateSet.add(k)));
  const dates = Array.from(dateSet).sort();
  return dates.map((date) => {
    const row = { date } as { date: string } & Record<string, number>;
    succeeded.forEach((r, i) => {
      const v = maps[i].get(date);
      if (v !== undefined) row[r.id] = v;
    });
    return row;
  });
}

function pctText(raw: string | null): string {
  return raw == null ? "—" : raw;
}

function pctClass(raw: string | null): string {
  if (raw == null) return "";
  const n = Number(raw);
  if (!Number.isFinite(n) || n === 0) return "";
  return n > 0 ? "text-pos" : "text-neg";
}

/**
 * Overlays 2+ selected runs' equity curves and lines up their headline
 * metrics side by side (Phase 56).
 *
 * Fetches each selected run's FULL detail itself, one
 * `GET /api/backtest-runs/{id}` per id — the list endpoint only ever
 * returns summaries (`BacktestRunSummary`, no `equity_curve`), so a
 * detail-per-run fetch is the only way to get what the overlay needs. This
 * makes the component self-sufficient: it does not receive curves as a
 * prop and does not read anything the run list already fetched.
 *
 * The Recharts usage below is entirely local to this file, independent of
 * whatever chart the single-run detail page (built separately, out of
 * this file's scope) may or may not use.
 */
export function BacktestRunComparison({
  strategyId,
  versionId,
  selectedRunIds,
}: {
  strategyId: string;
  versionId: string;
  selectedRunIds: string[];
}) {
  const [runs, setRuns] = useState<BacktestRunDetail[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  const selectionKey = selectedRunIds.join(",");

  useEffect(() => {
    let cancelled = false;

    // The whole body is deferred by one microtask so that NO `setState`
    // runs synchronously while React is still committing this effect
    // (`react-hooks` flags that as a cascading render). An `async` IIFE
    // alone would not be enough: an async function runs synchronously up
    // to its first `await`, so the resets below would still land inside
    // the effect. `Promise.resolve().then(...)` is what actually moves
    // them off it.
    void Promise.resolve().then(async () => {
      if (cancelled) return;

      if (selectedRunIds.length < 2) {
        // Clears whatever a previous, larger selection left behind. The
        // render below already short-circuits to an EmptyNote at this
        // count, so this is about not showing stale runs if the selection
        // grows again - not about what is on screen right now.
        setRuns(null);
        setErrorDetail(null);
        setStatus(null);
        return;
      }

      setLoading(true);
      setErrorDetail(null);
      setStatus(null);
      try {
        const responses = await Promise.all(
          selectedRunIds.map((id) => fetch(`/api/backtest-runs/${id}`, { cache: "no-store" })),
        );
        if (cancelled) return;

        // A 401 on any of the parallel fetches means the session itself is
        // gone — that takes priority over any other run's own error.
        const expired = responses.find((r) => r.status === 401);
        if (expired && handleExpiredSession(expired.status)) return;

        const bodies = await Promise.all(responses.map((r) => r.json().catch(() => null)));
        if (cancelled) return;

        const failedIndex = responses.findIndex((r) => !r.ok);
        if (failedIndex !== -1) {
          setRuns(null);
          setStatus(responses[failedIndex].status);
          setErrorDetail(
            formatDetail((bodies[failedIndex] as { detail?: unknown } | null)?.detail) ??
              `Request failed (HTTP ${responses[failedIndex].status})`,
          );
          return;
        }

        setRuns(bodies as BacktestRunDetail[]);
      } catch {
        if (!cancelled) {
          setRuns(null);
          setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    });

    return () => {
      cancelled = true;
    };
    // Re-fetches only when the actual set of selected ids changes, not on
    // every render of a new `selectedRunIds` array reference with the same
    // contents.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectionKey]);

  if (selectedRunIds.length < 2) {
    return (
      <EmptyNote>
        Select at least two runs above to compare their equity curves and metrics.
      </EmptyNote>
    );
  }

  const succeededRuns = runs ? runs.filter((r) => r.status === "succeeded") : [];
  const series = runs ? buildComparisonSeries(runs) : [];

  return (
    <Panel
      title="Compare runs"
      description={`Overlays each of the ${selectedRunIds.length} selected runs' equity curves (version ${versionId}, strategy ${strategyId}) and lines up their headline metrics. A failed run has nothing to plot, so it is excluded from the chart but still listed in the table below with its real error.`}
    >
      {loading && <EmptyNote>Loading {selectedRunIds.length} runs…</EmptyNote>}

      {errorDetail && (
        <Alert tone="error">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </Alert>
      )}

      {runs && !errorDetail && (
        <div className="flex flex-col gap-4">
          {succeededRuns.length > 0 ? (
            <div
              className="rounded-md border border-line bg-well p-3"
              data-testid="comparison-chart"
            >
              <ResponsiveContainer width="100%" height={320}>
                <LineChart data={series}>
                  <CartesianGrid stroke="var(--grid)" strokeDasharray="3 3" />
                  <XAxis
                    dataKey="date"
                    stroke="var(--ink-faint)"
                    tick={{ fill: "var(--ink-faint)", fontSize: 11 }}
                  />
                  <YAxis
                    stroke="var(--ink-faint)"
                    tick={{ fill: "var(--ink-faint)", fontSize: 11 }}
                  />
                  <Tooltip
                    contentStyle={{
                      background: "var(--surface)",
                      border: "1px solid var(--line)",
                      color: "var(--ink)",
                    }}
                  />
                  {succeededRuns.map((r, i) => (
                    <Line
                      key={r.id}
                      type="monotone"
                      dataKey={r.id}
                      name={runLabel(r)}
                      stroke={seriesColor(i)}
                      dot={false}
                      isAnimationActive={false}
                    />
                  ))}
                </LineChart>
              </ResponsiveContainer>
              {/*
                A hand-rolled legend rather than Recharts' own `<Legend>`:
                with 2+ `<Line>` series, `<Legend>` reliably suppresses
                every other Cartesian child (`<Line>`, `<YAxis>`,
                `<CartesianGrid>`) from rendering at all in this app's test
                environment (recharts 3.10.1 + jsdom) — verified directly
                against the installed package, not assumed. This list is
                keyed to the exact same `seriesColor(i)` each `<Line>`
                above uses, so it can never drift out of sync with the
                chart's real colours.
              */}
              <ul
                className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-ink-muted"
                data-testid="comparison-legend"
              >
                {succeededRuns.map((r, i) => (
                  <li key={r.id} className="flex items-center gap-1.5">
                    <span
                      aria-hidden="true"
                      className="inline-block h-2.5 w-2.5 rounded-full"
                      style={{ background: seriesColor(i) }}
                    />
                    {runLabel(r)}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <EmptyNote>
              None of the selected runs succeeded, so there is no equity curve to chart.
            </EmptyNote>
          )}

          <TableScroll>
            <table className={tableClass}>
              <caption className="sr-only">
                Headline metrics for every selected run, exactly as returned by the API
              </caption>
              <thead>
                <tr className={theadRowClass}>
                  <th className={thClass}>Metric</th>
                  {runs.map((r) => (
                    <th key={r.id} className={thClass}>
                      {runLabel(r)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr className={tbodyRowClass}>
                  <td className={tdClass}>total_return_pct</td>
                  {runs.map((r) => (
                    <td
                      key={r.id}
                      className={`${tdClass} ${pctClass(r.total_return_pct)}`}
                      data-testid={`cmp-return-${r.id}`}
                    >
                      {pctText(r.total_return_pct)}
                    </td>
                  ))}
                </tr>
                <tr className={tbodyRowClass}>
                  <td className={tdClass}>max_drawdown_pct</td>
                  {runs.map((r) => (
                    <td key={r.id} className={tdClass} data-testid={`cmp-drawdown-${r.id}`}>
                      {pctText(r.max_drawdown_pct)}
                    </td>
                  ))}
                </tr>
                <tr className={tbodyRowClass}>
                  <td className={tdClass}>win_rate_pct</td>
                  {runs.map((r) => (
                    <td key={r.id} className={tdClass} data-testid={`cmp-winrate-${r.id}`}>
                      {pctText(r.win_rate_pct)}
                    </td>
                  ))}
                </tr>
                <tr className={tbodyRowClass}>
                  <td className={tdClass}>num_trades</td>
                  {runs.map((r) => (
                    <td key={r.id} className={tdClass} data-testid={`cmp-trades-${r.id}`}>
                      {r.num_trades == null ? "—" : r.num_trades}
                    </td>
                  ))}
                </tr>
                <tr className={tbodyRowClass}>
                  <td className={tdClass}>final_equity</td>
                  {runs.map((r) => (
                    <td key={r.id} className={tdClass} data-testid={`cmp-equity-${r.id}`}>
                      {pctText(r.final_equity)}
                    </td>
                  ))}
                </tr>
                <tr className={tbodyRowClass}>
                  <td className={tdClass}>chart</td>
                  {runs.map((r) => (
                    <td
                      key={r.id}
                      className={`${tdClass} text-ink-muted`}
                      data-testid={`cmp-chart-status-${r.id}`}
                    >
                      {r.status === "succeeded" ? "plotted" : "excluded (run failed)"}
                    </td>
                  ))}
                </tr>
                <tr className={tbodyRowClass}>
                  <td className={tdClass}>status / error_detail</td>
                  {runs.map((r) => (
                    <td
                      key={r.id}
                      className={`${tdClass} text-ink-muted`}
                      data-testid={`cmp-status-${r.id}`}
                    >
                      {r.status === "failed" ? (r.error_detail ?? "failed") : r.status}
                    </td>
                  ))}
                </tr>
              </tbody>
            </table>
          </TableScroll>
        </div>
      )}
    </Panel>
  );
}

export default BacktestRunComparison;
