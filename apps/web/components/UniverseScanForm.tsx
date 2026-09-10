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
 * One universe scan's identity, inputs and roll-up counts — the backend's
 * `UniverseScanSummary` (Phase 60). Every numeric field the backend sends
 * as a JSON string is a `Decimal`; the roll-up counts (`num_*`) are real
 * integers, and each is `null`, never `0`, while the scan is still
 * `"running"` or if it `"failed"` outright — a fabricated zero would
 * misreport a scan that produced no result (docs/TRADING_SAFETY.md).
 *
 * `UniverseScanList.tsx` and `UniverseScanResults.tsx` both import these
 * types rather than restating them, so the files can never drift into
 * disagreeing about a scan's shape.
 */
export type UniverseScanSummary = {
  id: string;
  strategy_version_id: string;
  bar_interval: string;
  start_date: string;
  end_date: string;
  starting_cash: string;
  scan_mode: "explicit_list" | "all_ingested";
  requested_symbols: string[] | null;
  status: "running" | "succeeded" | "failed";
  num_symbols: number | null;
  num_succeeded: number | null;
  num_qualified: number | null;
  error_detail: string | null;
  created_at: string;
  completed_at: string | null;
};

/** One per-symbol result inside a scan's detail (`UniverseScanResultResponse`).
 * Every metric is `null` on a `"failed"` per-symbol result (a symbol with
 * no ingested bars over the window) — render `—`, never `0%`. */
export type UniverseScanResultResponse = {
  id: string;
  symbol: string;
  backtest_run_id: string;
  status: "succeeded" | "failed";
  total_return_pct: string | null;
  max_drawdown_pct: string | null;
  win_rate_pct: string | null;
  num_trades: number | null;
  error_detail: string | null;
  /** 1-indexed among succeeded by return desc; `null` for a failed symbol. */
  rank: number | null;
};

/** `UniverseScanDetailResponse` — a summary plus its ranked results, which
 * already arrive ordered (succeeded best-first by rank, then failed). */
export type UniverseScanDetailResponse = UniverseScanSummary & {
  results: UniverseScanResultResponse[];
};

export type ListUniverseScansResponse = {
  items?: UniverseScanSummary[];
  limit?: number;
  offset?: number;
};

/**
 * FastAPI returns a plain string `detail` for hand-raised `HTTPException`s
 * (a 409 `VERSION_NOT_VALIDATED: ...`, and every 422 this endpoint raises
 * by hand) but a list of objects for a request-shape validation error.
 * Mirrors `RunBacktestForm.formatDetail` / `StrategyList.formatDetail`.
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

/** Today in UTC, as `YYYY-MM-DD`. Mirrors `RunBacktestForm.todayUtc`. */
function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

/** `days` calendar days before today (UTC). Mirrors `RunBacktestForm.utcDaysAgo`. */
function utcDaysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

/**
 * Parses the symbols textarea: one per line, or comma-separated, or both.
 * Trims every token and drops blanks. An empty result means "the user
 * typed nothing usable", which the form treats as the "scan all ingested"
 * intent (send `symbols: null`).
 */
export function parseSymbols(raw: string): string[] {
  return raw
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

/**
 * Triggers a universe scan (Phase 60) against one validated strategy
 * version, via
 * `POST /api/strategies/{strategyId}/versions/{versionId}/universe-scans`.
 *
 * Two scan modes, one textarea plus one explicit checkbox:
 *
 *  - Type symbols (newline- or comma-separated) → an explicit list is
 *    sent. The backend caps the list at 50 and 422s past that.
 *  - Check "scan all ingested symbols" → the textarea is disabled and
 *    ignored, and `symbols: null` is sent ("scan every ingested symbol").
 *    An empty textarea implies the same thing, but the checkbox makes the
 *    intent unambiguous rather than relying on an empty field.
 *
 * `bar_interval` is hardcoded to `"1d"` and never exposed as a choice —
 * the only interval anything ingests or evaluates today, matching
 * `RunBacktestForm`.
 *
 * A 409 (`VERSION_NOT_VALIDATED: ...`) or a 422 (`symbols: []`, 51+
 * symbols, or nothing ingested) carries a plain-string `detail` that is
 * rendered verbatim, never paraphrased. A 201 is a real, persisted scan:
 * `onScanComplete` fires with it so the list below refreshes, and a brief
 * inline confirmation shows its current `status`.
 */
export function UniverseScanForm({
  strategyId,
  versionId,
  onScanComplete,
}: {
  strategyId: string;
  versionId: string;
  onScanComplete: (scan: UniverseScanDetailResponse) => void;
}) {
  const [symbolsText, setSymbolsText] = useState("");
  const [allIngested, setAllIngested] = useState(false);
  const [startDate, setStartDate] = useState(utcDaysAgo(180));
  const [endDate, setEndDate] = useState(todayUtc());
  const [startingCash, setStartingCash] = useState("100000");

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<UniverseScanDetailResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    const parsed = parseSymbols(symbolsText);
    // Explicit checkbox, or an empty textarea, both mean "scan all ingested".
    const scanAll = allIngested || parsed.length === 0;

    try {
      const res = await fetch(
        `/api/strategies/${strategyId}/versions/${versionId}/universe-scans`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            symbols: scanAll ? null : parsed,
            // Hardcoded — see the docblock above. Never a form field.
            bar_interval: "1d",
            start_date: startDate,
            end_date: endDate,
            starting_cash: startingCash,
          }),
        },
      );
      const data = (await res.json().catch(() => null)) as
        | (Partial<UniverseScanDetailResponse> & { detail?: unknown })
        | null;
      setStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setErrorDetail(
          formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`,
        );
        return;
      }
      const scan = data as UniverseScanDetailResponse;
      setResult(scan);
      onScanComplete(scan);
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Panel
      title="Run a universe scan"
      description={
        <>
          Runs this validated version&apos;s rules over many symbols at once and ranks
          them by return. Leave the list empty (or tick{" "}
          <em>scan all ingested symbols</em>) to scan every ingested symbol;
          otherwise type up to 50.{" "}
          <code className="font-mono text-ink-muted">bar_interval</code> is always{" "}
          <code className="font-mono text-ink-muted">1d</code>.
        </>
      }
    >
      <form onSubmit={handleSubmit} className="grid gap-3">
        <Field
          label="Symbols"
          hint="One per line or comma-separated, e.g. AAPL.US, MSFT.US. Max 50."
        >
          <textarea
            className={`${monoInputClass} min-h-24 resize-y`}
            value={symbolsText}
            onChange={(e) => setSymbolsText(e.target.value)}
            disabled={allIngested}
            placeholder={"AAPL.US\nMSFT.US\nNVDA.US"}
          />
        </Field>

        <label className="flex items-center gap-2 text-sm text-ink">
          <input
            type="checkbox"
            checked={allIngested}
            onChange={(e) => setAllIngested(e.target.checked)}
          />
          Scan all ingested symbols
        </label>

        <div className="grid gap-3 md:grid-cols-3">
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
        </div>

        <div>
          <button type="submit" disabled={loading} className={btnPrimary}>
            {loading ? "Scanning…" : "Run scan"}
          </button>
        </div>
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
          data-testid="scan-result"
        >
          <Pill
            tone={
              result.status === "succeeded"
                ? "pos"
                : result.status === "failed"
                  ? "neg"
                  : "warn"
            }
            testId="scan-status-pill"
          >
            {result.status}
          </Pill>
          <span className={hintClass}>
            Scan recorded — open it from the list below for the ranked per-symbol
            results.
          </span>
        </div>
      )}
    </Panel>
  );
}

export default UniverseScanForm;
