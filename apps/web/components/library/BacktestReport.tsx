"use client";

import {
  Alert,
  Pill,
  TableScroll,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";
import type { BacktestResult, Metrics } from "./types";
import { num, pct } from "./types";

/**
 * One library backtest, exactly as the API measured it (Phase 108, D135).
 * The verdict comes first and is the backend's fixed rule, not a judgement
 * made here. Strategy and buy-and-hold sit side by side in every table so
 * a return is never read without the thing it has to beat.
 */

const W = 640;
const H = 220;
const PAD = 10;

const VERDICT_TONE: Record<string, "pos" | "neg" | "warn" | "neutral"> = {
  beats_benchmark: "pos",
  lower_risk: "warn",
  survivorship_biased: "warn",
  no_edge: "neg",
  insufficient_history: "neutral",
};

/** Both curves on a shared log scale, so a 10x and a 2x are comparable. */
export function EquityChart({ curve }: { curve: BacktestResult["curve"] }) {
  const values = curve.flatMap((p) => (p.b !== null ? [p.s, p.b] : [p.s])).filter((v) => v > 0);
  if (curve.length < 2 || values.length < 2) return null;
  const lo = Math.log(Math.min(...values));
  const hi = Math.log(Math.max(...values));
  const span = hi - lo || 1;
  const x = (i: number) => PAD + (i / (curve.length - 1)) * (W - 2 * PAD);
  const y = (v: number) => PAD + (1 - (Math.log(v) - lo) / span) * (H - 2 * PAD);
  const line = (pick: (p: BacktestResult["curve"][number]) => number | null) =>
    curve
      .map((p, i) => {
        const v = pick(p);
        return v !== null && v > 0 ? `${x(i).toFixed(1)},${y(v).toFixed(1)}` : null;
      })
      .filter(Boolean)
      .join(" ");
  const bench = line((p) => p.b);
  return (
    <figure className="flex flex-col gap-2">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label="Growth of 1 unit: strategy against buy-and-hold, log scale"
        className="w-full rounded-md border border-line bg-well"
      >
        {[0.25, 0.5, 0.75].map((f) => (
          <line key={f} x1={PAD} x2={W - PAD} y1={PAD + f * (H - 2 * PAD)} y2={PAD + f * (H - 2 * PAD)} stroke="var(--grid)" strokeWidth="1" />
        ))}
        {bench ? <polyline points={bench} fill="none" stroke="var(--ink-faint)" strokeWidth="1.5" strokeDasharray="4 3" /> : null}
        <polyline points={line((p) => p.s)} fill="none" stroke="var(--accent)" strokeWidth="2" />
      </svg>
      <figcaption className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-faint">
        <span className="inline-flex items-center gap-1.5"><span className="inline-block h-0.5 w-5 bg-accent" />strategy ×{curve[curve.length - 1].s.toFixed(2)}</span>
        {bench ? (
          <span className="inline-flex items-center gap-1.5"><span className="inline-block h-0 w-5 border-t border-dashed border-ink-faint" />buy-and-hold ×{(curve[curve.length - 1].b ?? 0).toFixed(2)}</span>
        ) : null}
        <span>{curve[0].d} → {curve[curve.length - 1].d} · growth of 1, log scale</span>
      </figcaption>
    </figure>
  );
}

const ROWS: [string, (m: Metrics) => string][] = [
  ["Return a year", (m) => pct(m.cagr)],
  ["Volatility a year", (m) => pct(m.volatility).replace("+", "")],
  ["Sharpe", (m) => num(m.sharpe)],
  ["Sortino", (m) => num(m.sortino)],
  ["Worst drawdown", (m) => pct(m.max_drawdown)],
  ["Return ÷ drawdown", (m) => num(m.calmar)],
  ["Best / worst year", (m) => `${pct(m.best_year, 0)} / ${pct(m.worst_year, 0)}`],
  ["Positive years", (m) => `${m.positive_years} of ${m.total_years}`],
  ["t-statistic", (m) => num(m.t_stat)],
];

export default function BacktestReport({ result }: { result: BacktestResult }) {
  const tone = VERDICT_TONE[result.verdict.code] ?? "neutral";
  return (
    <div className="flex flex-col gap-5" data-testid="library-report">
      <div className="rounded-lg border border-line bg-surface p-4">
        <div className="mb-1.5 flex flex-wrap items-center gap-2">
          <Pill tone={tone} testId="library-verdict">{result.verdict.code.replaceAll("_", " ")}</Pill>
          <span className="text-sm font-semibold text-ink">{result.verdict.headline}</span>
        </div>
        <p className="text-sm leading-relaxed text-ink-muted">{result.verdict.detail}</p>
      </div>

      <EquityChart curve={result.curve} />

      <TableScroll>
        <table className={tableClass}>
          <thead>
            <tr className={theadRowClass}>
              <th className={thClass}>measure</th>
              {result.segments.map((s) => (
                <th key={s.label} className={thClass} colSpan={2}>{s.label}</th>
              ))}
            </tr>
            <tr className={theadRowClass}>
              <th className={thClass} />
              {result.segments.map((s) => (
                <SegmentHead key={s.label} />
              ))}
            </tr>
          </thead>
          <tbody>
            <tr className={tbodyRowClass}>
              <td className={tdClass}>Period</td>
              {result.segments.map((s) => (
                <td key={s.label} className={tdClass} colSpan={2}>
                  {s.strategy ? `${s.strategy.start} → ${s.strategy.end} (${s.strategy.years}y)` : "—"}
                </td>
              ))}
            </tr>
            {ROWS.map(([label, fmt]) => (
              <tr key={label} className={tbodyRowClass}>
                <td className={tdClass}>{label}</td>
                {result.segments.map((s) => (
                  <Cells key={s.label} a={s.strategy ? fmt(s.strategy) : "—"} b={s.benchmark ? fmt(s.benchmark) : "—"} />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
      <p className="text-xs text-ink-faint">
        {result.trades} rebalances · traded {num(result.turnover_per_year, 1)}× the portfolio a year ·
        costs took {pct(result.cost_drag_per_year, 2).replace("+", "")} a year · invested {pct(result.avg_exposure, 0).replace("+", "")} of
        the time on average · decisions trade {result.lag} session{result.lag === 1 ? "" : "s"} later.
      </p>

      <div className="grid gap-5 lg:grid-cols-2">
        <div className="rounded-lg border border-line bg-surface p-4">
          <h3 className="mb-1 text-sm font-semibold text-ink">What the rule holds now</h3>
          <p className="mb-3 text-xs text-ink-faint">
            {result.decided_on ? `Last decision ${result.decided_on}` : "No decision yet"} · data to {result.as_of}
          </p>
          {result.target.length === 0 ? (
            <p className="text-sm text-ink-muted">Nothing. The rule is entirely in cash.</p>
          ) : (
            <ul className="flex flex-col gap-2" data-testid="library-holdings">
              {result.target.map((h) => (
                <li key={h.symbol} className="flex flex-col gap-0.5 rounded-md bg-well px-3 py-2">
                  <span className="flex items-center justify-between font-mono text-xs text-ink">
                    <span>{h.symbol}</span>
                    <span className={h.weight > 0 ? "text-pos" : h.weight < 0 ? "text-neg" : "text-ink-faint"}>
                      {h.weight === 0 ? "cash" : `${(h.weight * 100).toFixed(0)}%`}
                    </span>
                  </span>
                  {h.note ? <span className="text-xs text-ink-muted">{h.note}</span> : null}
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="rounded-lg border border-line bg-surface p-4">
          <h3 className="mb-3 text-sm font-semibold text-ink">Year by year</h3>
          <div className="max-h-64 overflow-y-auto">
            <table className={tableClass}>
              <thead>
                <tr className={theadRowClass}>
                  <th className={thClass}>year</th>
                  <th className={thClass}>strategy</th>
                  <th className={thClass}>buy-and-hold</th>
                </tr>
              </thead>
              <tbody>
                {[...result.years].reverse().map((y) => (
                  <tr key={y.year} className={tbodyRowClass}>
                    <td className={tdClass}>{y.year}</td>
                    <td className={`${tdClass} ${y.strategy >= 0 ? "text-pos" : "text-neg"}`}>{pct(y.strategy)}</td>
                    <td className={tdClass}>{pct(y.benchmark)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {result.warnings.length > 0 ? (
        <Alert tone="warn" role="status" testId="library-warnings">
          <ul className="flex list-disc flex-col gap-1 pl-4">
            {result.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </Alert>
      ) : null}

      <details className="rounded-lg border border-line bg-surface p-4">
        <summary className="cursor-pointer text-sm font-semibold text-ink">
          Data ({result.source}, {result.data.length} symbol{result.data.length === 1 ? "" : "s"})
        </summary>
        <TableScroll>
          <table className={`${tableClass} mt-3`}>
            <thead>
              <tr className={theadRowClass}>
                <th className={thClass}>symbol</th>
                <th className={thClass}>vendor</th>
                <th className={thClass}>from</th>
                <th className={thClass}>to</th>
                <th className={thClass}>bars</th>
                <th className={thClass}>last close</th>
                <th className={thClass}>cost (bps)</th>
                <th className={thClass}>screened</th>
              </tr>
            </thead>
            <tbody>
              {result.data.map((d) => (
                <tr key={d.symbol} className={tbodyRowClass}>
                  <td className={tdClass}>{d.symbol}</td>
                  <td className={tdClass}>{d.vendor_symbol}</td>
                  <td className={tdClass}>{d.first}</td>
                  <td className={tdClass}>{d.last}</td>
                  <td className={tdClass}>{d.bars}</td>
                  <td className={tdClass}>{d.last_close.toFixed(d.last_close < 10 ? 4 : 2)} {d.currency ?? ""}</td>
                  <td className={tdClass}>{d.cost_bps}</td>
                  <td className={tdClass}>
                    {d.removed.length === 0 && !d.truncated_before
                      ? "clean"
                      : [
                          d.removed.length ? `${d.removed.length} bad tick(s) dropped` : "",
                          d.truncated_before ? `history before ${d.truncated_before} discarded` : "",
                        ]
                          .filter(Boolean)
                          .join("; ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      </details>
    </div>
  );
}

function SegmentHead() {
  return (
    <>
      <th className={thClass}>strategy</th>
      <th className={`${thClass} text-ink-faint`}>buy-and-hold</th>
    </>
  );
}

function Cells({ a, b }: { a: string; b: string }) {
  return (
    <>
      <td className={tdClass}>{a}</td>
      <td className={`${tdClass} text-ink-muted`}>{b}</td>
    </>
  );
}
