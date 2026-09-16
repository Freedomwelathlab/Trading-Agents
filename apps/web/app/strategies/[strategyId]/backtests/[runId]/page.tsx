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
import { PriceChart, type PriceBar, type SignalMarker } from "@/components/PriceChart";
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
  /**
   * Phase 70 cost fields. Optional in this type as well as nullable: a run
   * created before D088 has no cost annotation at all, and `null` there
   * means "frictionless, unknown costs" rather than zero — so these render
   * as an em dash, never as 0, exactly like every other null metric here.
   */
  fee_bps?: string | null;
  slippage_bps?: string | null;
  total_fees?: string | null;
  total_slippage?: string | null;
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
  const [bars, setBars] = useState<PriceBar[]>([]);
  const [barsNote, setBarsNote] = useState<string | null>(null);
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

  /**
   * Bars are fetched SEPARATELY, after the run loads, and a failure here is
   * deliberately NOT promoted to the page's error state: a run's metrics,
   * equity curve and ledger are complete without a price chart, and losing
   * the whole page because the bar store has a gap would hide the result
   * the user came for. The chart area says what happened instead.
   *
   * The window asked for is exactly the run's own `[start_date, end_date]`,
   * so the candles shown are the bars the engine replayed — not a wider or
   * more recent view that would put markers on bars the backtest never saw.
   */
  useEffect(() => {
    if (!run || run.status !== "succeeded") return;
    void (async () => {
      setBarsNote(null);
      const query = new URLSearchParams({
        start_date: run.start_date,
        end_date: run.end_date,
        bar_interval: run.bar_interval,
      });
      try {
        const res = await fetch(
          `/api/market-data/${encodeURIComponent(run.symbol)}/bars?${query}`,
          { cache: "no-store" },
        );
        const data = (await res.json().catch(() => null)) as
          | { bars?: PriceBar[]; detail?: unknown }
          | null;
        if (!res.ok) {
          if (handleExpiredSession(res.status)) return;
          setBars([]);
          setBarsNote(
            formatDetail(data?.detail) ?? `Could not load bars (HTTP ${res.status})`,
          );
          return;
        }
        setBars(data?.bars ?? []);
      } catch {
        setBars([]);
        setBarsNote("DATA_UNAVAILABLE: could not reach the trading API");
      }
    })();
  }, [run]);

  /**
   * One marker per LEG, not per trade: a round trip is two things that
   * happened on two different bars, and collapsing it to a single marker at
   * its entry would leave every exit unmarked. Prices are the executed
   * ones the ledger stores — which, from Phase 70 on, are net of costs, so
   * a marker's label agrees with the equity curve rather than with the raw
   * close beneath it.
   */
  const signalMarkers: SignalMarker[] =
    run?.status === "succeeded"
      ? run.trades.flatMap((t) => [
          { date: t.entry_date, side: "buy" as const, price: t.entry_price },
          { date: t.exit_date, side: "sell" as const, price: t.exit_price },
        ])
      : [];

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

                <SectionHeading>Price &amp; signals</SectionHeading>
                <Panel
                  title={`${run.symbol} — ${run.bar_interval}`}
                  actions={
                    <Pill tone="neutral">
                      {signalMarkers.length} signal{signalMarkers.length === 1 ? "" : "s"}
                    </Pill>
                  }
                >
                  {barsNote ? (
                    <Alert tone="warn" testId="bars-note">
                      {barsNote} — the run&rsquo;s own results below are unaffected.
                    </Alert>
                  ) : (
                    <PriceChart bars={bars} signals={signalMarkers} />
                  )}
                </Panel>

                <SectionHeading>Costs</SectionHeading>
                <Panel title="Transaction costs">
                  {run.fee_bps == null && run.slippage_bps == null ? (
                    <EmptyNote>
                      This run predates cost modelling, so its returns are frictionless — no
                      fee, no spread, no slippage. That is not the same as zero cost, and the
                      figures above should be read as an upper bound.
                    </EmptyNote>
                  ) : (
                    <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                      <Stat label="fee_bps" value={run.fee_bps ?? "—"} testId="run-fee-bps" />
                      <Stat
                        label="slippage_bps"
                        value={run.slippage_bps ?? "—"}
                        testId="run-slippage-bps"
                      />
                      <Stat
                        label="total_fees"
                        value={run.total_fees ?? "—"}
                        tone="neg"
                        testId="run-total-fees"
                      />
                      <Stat
                        label="total_slippage"
                        value={run.total_slippage ?? "—"}
                        tone="neg"
                        testId="run-total-slippage"
                      />
                    </div>
                  )}
                </Panel>

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
