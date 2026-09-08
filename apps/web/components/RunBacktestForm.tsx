"use client";

import { useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  Field,
  Panel,
  Pill,
  btnPrimary,
  hintClass,
  inputClass,
  monoInputClass,
} from "@/components/ui/primitives";

/**
 * One backtest run's identity, inputs and metrics — `BacktestRunSummary`
 * from `apps/api/app/api/schemas_strategy_backtests.py`. Every metric is
 * `null`, never `0`, on a run whose `status` is `"failed"`: the backend
 * never computed one, and reporting a fabricated zero would misreport a
 * run that produced no result at all (docs/TRADING_SAFETY.md).
 *
 * `BacktestRunList.tsx` and `BacktestRunComparison.tsx` both import this
 * type rather than restating it, so the three files can never drift into
 * disagreeing about what a run summary looks like.
 */
export type BacktestRunSummary = {
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
};

/**
 * FastAPI returns a plain string `detail` for hand-raised `HTTPException`s
 * (a 409 `VERSION_NOT_VALIDATED: ...` in particular) but a list of objects
 * for a 422 request-shape validation error. Mirrors
 * `BacktestPanel.formatDetail` / `StrategyList.formatDetail`.
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

/** Today in UTC, as `YYYY-MM-DD`. Mirrors `BacktestPanel.todayUtc`. */
function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

/** `days` calendar days before today (UTC), as `YYYY-MM-DD`. Mirrors
 * `BacktestPanel.utcDaysAgo` — used only for a sensible default window,
 * since (unlike D025's ephemeral panel) a persisted run has no
 * `end_date` == today constraint. */
function utcDaysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

/**
 * Colours a percentage figure from the sign of the number the backend
 * actually returned. Mirrors `PortfolioView.pnlTone` / `BacktestPanel.returnTone`.
 */
function returnTone(raw: string | null | undefined): "pos" | "neg" | undefined {
  if (raw == null) return undefined;
  const n = Number(raw);
  if (!Number.isFinite(n) || n === 0) return undefined;
  return n > 0 ? "pos" : "neg";
}

/**
 * Triggers a persisted backtest run (Phase 56) against one strategy
 * version, via `POST /api/strategies/{strategyId}/versions/{versionId}/backtests`.
 *
 * `bar_interval` is hardcoded to `"1d"` and never exposed as a choice: the
 * backend's `CreateBacktestRunRequest.bar_interval` is a `Literal["1d"]`
 * (schemas_strategy_backtests.py) — it is the only interval anything
 * ingests or evaluates today, and a selector implying other intervals work
 * would be misleading.
 *
 * A 201 response is not automatically success. The backend persists a run
 * either way and reports its real outcome via `status` —
 * `"succeeded"` or `"failed"` — so both are rendered as what they are,
 * never collapsed into one green confirmation: a failed run shows its real
 * `error_detail`, not a fabricated result. A 409 means the selected
 * version is not `"validated"` (`VERSION_NOT_VALIDATED: ...`) and is
 * rendered verbatim, not paraphrased.
 *
 * This panel deliberately does not render the new run's equity curve or
 * trade ledger — that is the single-run detail page's job. It shows just
 * enough (status + headline number, or the error) to confirm the run
 * happened, then leaves the reader to open the detail page from the list
 * below.
 */
export function RunBacktestForm({
  strategyId,
  versionId,
  onRunComplete,
}: {
  strategyId: string;
  versionId: string;
  onRunComplete: (run: BacktestRunSummary) => void;
}) {
  const [symbol, setSymbol] = useState("AAPL.US");
  const [startDate, setStartDate] = useState(utcDaysAgo(180));
  const [endDate, setEndDate] = useState(todayUtc());
  const [startingCash, setStartingCash] = useState("100000");

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<BacktestRunSummary | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    try {
      const res = await fetch(
        `/api/strategies/${strategyId}/versions/${versionId}/backtests`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            symbol,
            // Hardcoded — see the docblock above. Never a form field.
            bar_interval: "1d",
            start_date: startDate,
            end_date: endDate,
            starting_cash: startingCash,
          }),
        },
      );
      const data = (await res.json().catch(() => null)) as
        | (Partial<BacktestRunSummary> & { detail?: unknown })
        | null;
      setStatus(res.status);
      if (!res.ok) {
        // Real backend statuses only — 401 (session gone), 409
        // `VERSION_NOT_VALIDATED: ...` (this version cannot be backtested
        // yet), 422 (request-shape validation), 503 (proxy could not
        // reach the API). Never a placeholder result.
        if (handleExpiredSession(res.status)) return;
        setErrorDetail(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      const run = data as BacktestRunSummary;
      setResult(run);
      onRunComplete(run);
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  const tone = returnTone(result?.total_return_pct);
  const returnClass = tone === "pos" ? "text-pos" : tone === "neg" ? "text-neg" : "text-ink";

  return (
    <Panel
      title="Run a backtest"
      description={
        <>
          Runs this version&apos;s rules over real historical daily closes and persists
          the result. <code className="font-mono text-ink-muted">bar_interval</code> is
          always <code className="font-mono text-ink-muted">1d</code> — the only interval
          this engine evaluates today.
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
        <Field label="End date">
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
        <div
          className="flex flex-wrap items-center gap-3 rounded-md border border-line bg-well px-3 py-2.5"
          data-testid="run-result"
        >
          <Pill tone={result.status === "succeeded" ? "pos" : "neg"} testId="run-status-pill">
            {result.status}
          </Pill>
          {result.status === "succeeded" ? (
            <span className={`tnum font-mono text-sm ${returnClass}`} data-testid="run-total-return">
              total_return_pct {result.total_return_pct}
            </span>
          ) : (
            <span className="text-sm text-ink-muted" data-testid="run-error-detail">
              {result.error_detail ?? "Run failed with no error detail."}
            </span>
          )}
          <span className={hintClass}>
            Run recorded — open it from the list below for the full chart and trade ledger.
          </span>
        </div>
      )}
    </Panel>
  );
}

export default RunBacktestForm;
