"use client";

import { useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  Field,
  Panel,
  btnPrimary,
  hintClass,
  monoInputClass,
} from "@/components/ui/primitives";

/**
 * One persisted signal evaluation — the backend's `SignalEvaluationResponse`
 * (Phase 61). Every numeric field is serialized as a JSON string
 * (`Decimal`), or `null`. An `insufficient_data: true` row is a REAL, kept
 * result — its `signal` is always `"hold"`, its `latest_close` and
 * `indicator_values` are `—`, and its `explanation` says how many bars were
 * available versus how many the rules needed. It is never an error.
 *
 * `SignalTable.tsx` imports these types rather than restating them, so the
 * two files can never drift into disagreeing about a signal's shape.
 */
export type SignalDecision = "buy" | "sell" | "hold";

export type SignalEvaluationResponse = {
  id: string;
  strategy_version_id: string;
  symbol: string;
  bar_interval: string;
  /** ISO date; `null` only when zero bars were available. */
  as_of_bar_date: string | null;
  /** Decimal-as-string, or `null`. */
  latest_close: string | null;
  signal: SignalDecision;
  entry_rule_held: boolean | null;
  exit_rule_held: boolean | null;
  insufficient_data: boolean;
  /** Decimal-as-string per indicator, or `null` where the indicator isn't defined yet. */
  indicator_values: Record<string, string | null>;
  /** Always concrete, always the "why". Rendered verbatim, never truncated. */
  explanation: string;
  created_at: string;
};

/** `POST .../signals` returns one evaluation per requested symbol, in request order. */
export type SignalEvaluationBatchResponse = {
  items: SignalEvaluationResponse[];
};

export type ListSignalEvaluationsResponse = {
  items?: SignalEvaluationResponse[];
  limit?: number;
  offset?: number;
};

/**
 * FastAPI returns a plain string `detail` for hand-raised `HTTPException`s
 * (a 409 `VERSION_NOT_VALIDATED: ...`, and every 422 this endpoint raises
 * by hand) but a list of objects for a request-shape validation error.
 * Mirrors `UniverseScanForm.formatDetail` / `RunBacktestForm.formatDetail`.
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
 * Parses the symbols textarea: one per line, or comma-separated, or both.
 * Trims every token and drops blanks. Mirrors `UniverseScanForm.parseSymbols`.
 */
export function parseSymbols(raw: string): string[] {
  return raw
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

/**
 * Checks the current signal for a batch of symbols against one validated
 * strategy version, via
 * `POST /api/strategies/{strategyId}/versions/{versionId}/signals`.
 *
 * One textarea (newline- and/or comma-separated symbols, trimmed, blanks
 * dropped) and one submit button. Unlike a universe scan there is no "all
 * ingested" mode — the backend requires 1 to 50 explicit symbols and 422s
 * on `[]`, so an empty textarea is caught here with an inline validation
 * message and never POSTed.
 *
 * `bar_interval` is hardcoded to `"1d"` and never exposed as a choice — the
 * only interval anything ingests or evaluates today, matching
 * `RunBacktestForm` and `UniverseScanForm`.
 *
 * A 409 (`VERSION_NOT_VALIDATED: ...`) or a 422 carries a plain-string
 * `detail` that is rendered verbatim in an error `Alert`, never
 * paraphrased. A 201 is a real, persisted batch: `onEvaluated` fires with
 * its `items` so the table below refreshes, and a brief inline
 * confirmation shows how many symbols were evaluated.
 */
export function CheckSignalsForm({
  strategyId,
  versionId,
  onEvaluated,
}: {
  strategyId: string;
  versionId: string;
  onEvaluated: (items: SignalEvaluationResponse[]) => void;
}) {
  const [symbolsText, setSymbolsText] = useState("");
  const [loading, setLoading] = useState(false);
  const [evaluatedCount, setEvaluatedCount] = useState<number | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setEvaluatedCount(null);
    setErrorDetail(null);
    setStatus(null);
    setValidationError(null);

    const parsed = parseSymbols(symbolsText);
    if (parsed.length === 0) {
      setValidationError("Enter at least one symbol to check.");
      return;
    }
    if (parsed.length > 50) {
      setValidationError(
        `Enter at most 50 symbols — you entered ${parsed.length}.`,
      );
      return;
    }

    setLoading(true);
    try {
      const res = await fetch(
        `/api/strategies/${strategyId}/versions/${versionId}/signals`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            symbols: parsed,
            // Hardcoded — see the docblock above. Never a form field.
            bar_interval: "1d",
          }),
        },
      );
      const data = (await res.json().catch(() => null)) as
        | (Partial<SignalEvaluationBatchResponse> & { detail?: unknown })
        | null;
      setStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setErrorDetail(
          formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`,
        );
        return;
      }
      const items = data?.items ?? [];
      setEvaluatedCount(items.length);
      onEvaluated(items);
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Panel
      title="Check signals"
      description={
        <>
          Evaluates this validated version&apos;s rules against the latest ingested
          bars and tells you whether it currently says BUY, SELL or HOLD for each
          symbol — always with a plain-language reason. Type 1 to 50 symbols.{" "}
          <code className="font-mono text-ink-muted">bar_interval</code> is always{" "}
          <code className="font-mono text-ink-muted">1d</code>.
        </>
      }
    >
      <form onSubmit={handleSubmit} className="grid gap-3">
        <Field
          label="Symbols"
          hint="One per line or comma-separated, e.g. AAPL.US, MSFT.US. 1 to 50."
        >
          <textarea
            className={`${monoInputClass} min-h-24 resize-y`}
            value={symbolsText}
            onChange={(e) => setSymbolsText(e.target.value)}
            placeholder={"AAPL.US\nMSFT.US\nNVDA.US"}
          />
        </Field>

        <div>
          <button type="submit" disabled={loading} className={btnPrimary}>
            {loading ? "Checking…" : "Check signals"}
          </button>
        </div>
      </form>

      {validationError && (
        <Alert tone="warn" testId="signals-validation">
          {validationError}
        </Alert>
      )}

      {errorDetail && (
        <Alert tone="error">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </Alert>
      )}

      {evaluatedCount != null && !errorDetail && (
        <div
          className="flex flex-wrap items-center gap-3 rounded-md border border-line bg-well px-3 py-2.5"
          data-testid="signals-result"
        >
          <span className={hintClass}>
            Evaluated {evaluatedCount} symbol{evaluatedCount === 1 ? "" : "s"} — the
            current signal for each is in the table below.
          </span>
        </div>
      )}
    </Panel>
  );
}

export default CheckSignalsForm;
