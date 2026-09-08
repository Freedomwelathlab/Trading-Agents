"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { handleExpiredSession } from "@/lib/session";
import AppShell from "@/components/shell/AppShell";
import { computeDrawdownSeries, computeMonthlyReturns } from "@/lib/backtestMetrics";
import { EquityCurveChart } from "@/components/EquityCurveChart";
import { DrawdownChart } from "@/components/DrawdownChart";
import { MonthlyReturnsHeatmap } from "@/components/MonthlyReturnsHeatmap";
import { BacktestTradeLedger, type TradeRow } from "@/components/BacktestTradeLedger";
import {
  Alert,
  EmptyNote,
  KeyValue,
  Panel,
  Pill,
  SectionHeading,
  Stat,
  Term,
  Value,
} from "@/components/ui/primitives";

type EquityCurveEntry = { date: string; equity: string };

type BacktestRunDetailResponse = {
  id: string;
  strategy_version_id: string;
  symbol: string;
  bar_interval: string;
  start_date: string;
  end_date: string;
  starting_cash: string;
  status: "succeeded" | "failed";
  final_equity: string | null;
  total_return_pct: string | null;
  max_drawdown_pct: string | null;
  win_rate_pct: string | null;
  num_trades: number | null;
  error_detail: string | null;
  created_at: string;
  completed_at: string | null;
  equity_curve: EquityCurveEntry[];
  trades: TradeRow[];
};

/**
 * FastAPI returns a plain string `detail` for hand-raised `HTTPException`s
 * but a list of objects for a 422 request-shape validation error. Mirrors
 * `BacktestPanel.formatDetail` / `StrategyDetailPage.formatDetail`.
 */
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
 * Colours a signed percentage figure from the sign of the number the
 * backend actually returned. Mirrors `BacktestPanel.returnTone` /
 * `PortfolioView.pnlTone` — same convention everywhere a P&L-shaped figure
 * is coloured on this dashboard.
 */
function signTone(raw: string | null | undefined): "pos" | "neg" | undefined {
  const n = Number(raw);
  if (raw == null || raw === "" || !Number.isFinite(n) || n === 0) return undefined;
  return n > 0 ? "pos" : "neg";
}

function statusTone(status: string): "pos" | "neg" | "warn" | "neutral" {
  if (status === "succeeded") return "pos";
  if (status === "failed") return "neg";
  return "neutral";
}

/**
 * Single-run backtest detail (Phase 56/D0xx): the full result of one
 * persisted backtest run — `GET /backtest-runs/{runId}` — with its equity
 * curve, drawdown, monthly-returns heatmap and trade ledger.
 *
 * A `"failed"` run carries `null` for every metric field (not `0`, not an
 * omitted key) — this page renders that honestly: the real `error_detail`
 * in an error `Alert`, and nothing else metric- or chart-shaped, rather
 * than a chart with a flat/zero line standing in for "no data".
 *
 * Entirely a client component, `useParams`-driven, matching
 * `app/strategies/[strategyId]/page.tsx`'s shape rather than a
 * server-component-composes-client-panels split: the fetch, loading and
 * error state here have nothing else in this phase's scope to compose
 * with.
 */
export default function BacktestRunDetailPage() {
  const params = useParams<{ strategyId: string; runId: string }>();
  const { runId } = params;

  const [run, setRun] = useState<BacktestRunDetailResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  useEffect(() => {
    void (async () => {
      setLoading(true);
      setErrorDetail(null);
      setStatus(null);
      try {
        const res = await fetch(`/api/backtest-runs/${encodeURIComponent(runId)}`, {
          cache: "no-store",
        });
        const data = (await res.json().catch(() => null)) as
          | (BacktestRunDetailResponse & { detail?: unknown })
          | null;
        setStatus(res.status);
        if (!res.ok) {
          if (handleExpiredSession(res.status)) return;
          setRun(null);
          setErrorDetail(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
          return;
        }
        setRun(data);
      } catch {
        setRun(null);
        setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
      } finally {
        setLoading(false);
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    })();
  }, [runId]);

  const parsedCurve =
    run?.status === "succeeded"
      ? run.equity_curve.map((p) => ({ date: p.date, equity: Number(p.equity) }))
      : [];
  const drawdown = run?.status === "succeeded" ? computeDrawdownSeries(parsedCurve) : [];
  const monthlyReturns = run?.status === "succeeded" ? computeMonthlyReturns(parsedCurve) : [];

  return (
    <AppShell
      title={run ? `Backtest run — ${run.symbol}` : "Backtest run"}
      subtitle="Full result of one persisted backtest run: equity curve, drawdown, monthly returns and the trade ledger."
    >
      <div className="flex flex-col gap-5">
        {errorDetail && (
          <Alert tone="error">
            {status ? `HTTP ${status}: ` : ""}
            {errorDetail}
          </Alert>
        )}

        {loading && !run && !errorDetail && <EmptyNote>Loading backtest run…</EmptyNote>}

        {run && !errorDetail && (
          <>
            <Panel
              title="Run"
              actions={<Pill tone={statusTone(run.status)}>{run.status}</Pill>}
            >
              <KeyValue>
                <Term>symbol</Term>
                <Value testId="run-symbol">{run.symbol}</Value>
                <Term>bar_interval</Term>
                <Value>{run.bar_interval}</Value>
                <Term>start_date</Term>
                <Value>{run.start_date}</Value>
                <Term>end_date</Term>
                <Value>{run.end_date}</Value>
                <Term>starting_cash</Term>
                <Value testId="run-starting-cash">{run.starting_cash}</Value>
                <Term>created_at</Term>
                <Value>{run.created_at}</Value>
                <Term>completed_at</Term>
                <Value>{run.completed_at ?? "—"}</Value>
              </KeyValue>
            </Panel>

            {run.status === "failed" && (
              <Alert tone="error" testId="run-error-detail">
                {run.error_detail ?? "This run failed, but the backend returned no error_detail."}
              </Alert>
            )}

            {run.status === "succeeded" && (
              <>
                <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
                  <Stat
                    label="final_equity"
                    value={run.final_equity ?? "—"}
                    tone="accent"
                    testId="run-final-equity"
                  />
                  <Stat
                    label="total_return_pct"
                    value={run.total_return_pct ?? "—"}
                    tone={signTone(run.total_return_pct)}
                    testId="run-total-return"
                  />
                  <Stat
                    label="max_drawdown_pct"
                    value={run.max_drawdown_pct ?? "—"}
                    testId="run-max-drawdown"
                  />
                  <Stat
                    label="win_rate_pct"
                    value={run.win_rate_pct ?? "—"}
                    testId="run-win-rate"
                  />
                  <Stat
                    label="num_trades"
                    value={run.num_trades ?? "—"}
                    testId="run-num-trades"
                  />
                </div>

                <SectionHeading>Equity curve</SectionHeading>
                <Panel title="Equity curve">
                  <EquityCurveChart series={[{ name: run.symbol, points: parsedCurve }]} />
                </Panel>

                <SectionHeading>Drawdown</SectionHeading>
                <Panel title="Drawdown from peak">
                  <DrawdownChart points={drawdown} />
                </Panel>

                <SectionHeading>Monthly returns</SectionHeading>
                <Panel title="Monthly returns">
                  <MonthlyReturnsHeatmap months={monthlyReturns} />
                </Panel>

                <BacktestTradeLedger trades={run.trades} />
              </>
            )}
          </>
        )}
      </div>
    </AppShell>
  );
}
