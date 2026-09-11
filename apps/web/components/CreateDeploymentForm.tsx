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
 * The shapes the Phase 63 deployment routes return (D081). Kept here and
 * imported by `DeploymentList` / `DeploymentRunTable` / `DeploymentSignalTable`
 * rather than restated, so the components can never drift into disagreeing
 * about a deployment's shape (the same rule `CheckSignalsForm` follows).
 *
 * A deployment does NOTHING on its own: a fresh row is
 * `"pending_approval"`, the scheduled runner only ever touches `"active"`
 * rows, and the runner itself is off unless an operator has set
 * `STRATEGY_RUNNER_ENABLED`. `mode` is always `"paper"` this phase.
 */
export type DeploymentStatus =
  | "pending_approval"
  | "active"
  | "paused"
  | "stopped";

export type StrategyDeploymentResponse = {
  id: string;
  strategy_version_id: string;
  broker_id: string;
  mode: string;
  status: DeploymentStatus;
  symbols: string[];
  bar_interval: string;
  requested_by_user_id: string | null;
  approved_by_user_id: string | null;
  /** ISO timestamp; `null` until the deployment has been approved. */
  approved_at: string | null;
  paused_reason: string | null;
  stopped_at: string | null;
  /** ISO timestamp of the last runner cycle that evaluated this deployment. */
  last_evaluated_at: string | null;
  created_at: string;
  updated_at: string;
};

export type ListDeploymentsResponse = {
  items?: StrategyDeploymentResponse[];
  limit?: number;
  offset?: number;
};

export type DeploymentRunStatus =
  | "succeeded"
  | "failed"
  | "skipped_not_active"
  | "skipped_emergency_stop"
  | "skipped_market_closed"
  | "skipped_lock_held"
  // A `live` cycle on a server not armed for unattended live execution —
  // every default deployment (D082/D087). Present since Phase 64 on the
  // backend; it was missing from this union until Phase 69.
  | "skipped_live_trading_disabled"
  // An ARMED live cycle that stopped itself on a capital circuit breaker
  // and paused the deployment (Phase 69, D087). The opposite of the status
  // above: that one means the robot never ran, this one means it was
  // running on real money and halted.
  | "skipped_live_risk_halt";

export type StrategyDeploymentRunResponse = {
  id: string;
  deployment_id: string;
  status: DeploymentRunStatus;
  started_at: string;
  completed_at: string | null;
  symbols_evaluated: number;
  signals_actionable: number;
  orders_submitted: number;
  orders_filled: number;
  error_detail: string | null;
  created_at: string;
};

export type ListDeploymentRunsResponse = {
  items?: StrategyDeploymentRunResponse[];
  limit?: number;
  offset?: number;
};

export type DeploymentSignalDecision = "buy" | "sell" | "hold";

export type DeploymentSignalResponse = {
  id: string;
  deployment_run_id: string | null;
  symbol: string;
  /** ISO date; `null` only when zero bars were available. */
  as_of_bar_date: string | null;
  /** Decimal-as-string, or `null`. */
  latest_close: string | null;
  signal: DeploymentSignalDecision;
  insufficient_data: boolean;
  /** Always concrete, always the "why". Rendered verbatim, never truncated. */
  explanation: string;
  created_at: string;
};

export type ListDeploymentSignalsResponse = {
  items?: DeploymentSignalResponse[];
  limit?: number;
  offset?: number;
};

/** `insufficient_data` means a successful cycle ran but didn't yet have
 * enough closed round trips (or no reference backtest exists) to compare
 * against — a real, honest answer, not an error. */
export type DriftCheckStatus =
  | "no_drift"
  | "drift_detected"
  | "insufficient_data";

/** `paused` is the only value where the runner actually changed the
 * deployment's state on its own; `none` and `observed_only` are purely
 * informational. */
export type DriftCheckActionTaken = "none" | "observed_only" | "paused";

export type DriftCheckResponse = {
  id: string;
  deployment_id: string;
  status: DriftCheckStatus;
  /** Decimal-as-string; `null` for all three win-rate fields only when
   * `status === "insufficient_data"`. */
  actual_win_rate_pct: string | null;
  expected_win_rate_pct: string | null;
  win_rate_deviation_pct: string | null;
  num_round_trips: number;
  action_taken: DriftCheckActionTaken;
  /** Always concrete, always the "why" — rendered verbatim, never
   * truncated, regardless of `status`. */
  detail: string;
  created_at: string;
};

export type ListDriftChecksResponse = {
  items?: DriftCheckResponse[];
  limit?: number;
  offset?: number;
};

/**
 * FastAPI returns a plain string `detail` for hand-raised `HTTPException`s
 * (a 409 guardrail — `VERSION_NOT_VALIDATED`, `NO_SUCH_BROKER`,
 * `NOT_A_PAPER_BROKER`, `TOO_MANY_SYMBOLS`, `NOT_PENDING_APPROVAL`,
 * `NOT_ACTIVE`, `NOT_PAUSED`, `ALREADY_STOPPED`) but a list of objects for
 * a request-shape validation error. Mirrors `CheckSignalsForm.formatDetail`.
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
 * Trims every token and drops blanks. Mirrors `CheckSignalsForm.parseSymbols`.
 */
export function parseSymbols(raw: string): string[] {
  return raw
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Creates a deployment for one validated strategy version, via
 * `POST /api/strategies/{strategyId}/versions/{versionId}/deployments`.
 *
 * There is no broker-list endpoint, so the paper broker is a plain uuid
 * text field with a hint. `bar_interval` is hardcoded to `"1d"` and `mode`
 * to `"paper"` — the only values anything accepts today — and neither is
 * exposed as a choice, matching `CheckSignalsForm`.
 *
 * A 201 is a real, persisted row with `status: "pending_approval"`:
 * `onCreated` fires so the list below refreshes, and an inline note makes
 * clear the deployment is NOT trading yet and must be approved. A 409
 * guardrail or a 422 carries a plain-string `detail` rendered verbatim in
 * an error `Alert`, never paraphrased.
 */
export function CreateDeploymentForm({
  strategyId,
  versionId,
  onCreated,
}: {
  strategyId: string;
  versionId: string;
  onCreated: (deployment: StrategyDeploymentResponse) => void;
}) {
  const [brokerId, setBrokerId] = useState("");
  const [symbolsText, setSymbolsText] = useState("");
  const [loading, setLoading] = useState(false);
  const [createdStatus, setCreatedStatus] = useState<DeploymentStatus | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setCreatedStatus(null);
    setErrorDetail(null);
    setStatus(null);
    setValidationError(null);

    const trimmedBroker = brokerId.trim();
    if (trimmedBroker.length === 0) {
      setValidationError("Enter the paper broker's id.");
      return;
    }
    if (!UUID_RE.test(trimmedBroker)) {
      setValidationError("The broker id must be a UUID.");
      return;
    }

    const parsed = parseSymbols(symbolsText);
    if (parsed.length === 0) {
      setValidationError("Enter at least one symbol to deploy.");
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
        `/api/strategies/${strategyId}/versions/${versionId}/deployments`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            broker_id: trimmedBroker,
            symbols: parsed,
            // Hardcoded — see the docblock above. Never form fields.
            bar_interval: "1d",
            mode: "paper",
          }),
        },
      );
      const data = (await res.json().catch(() => null)) as
        | (Partial<StrategyDeploymentResponse> & { detail?: unknown })
        | null;
      setStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setErrorDetail(
          formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`,
        );
        return;
      }
      if (data && data.id) {
        setCreatedStatus(data.status ?? "pending_approval");
        onCreated(data as StrategyDeploymentResponse);
      }
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Panel
      title="Deploy this version"
      description={
        <>
          Puts this validated version on the scheduled paper-trading runner.
          The deployment is created{" "}
          <code className="font-mono text-ink-muted">pending_approval</code> and
          does nothing — no evaluation, no order — until you approve it below.{" "}
          <code className="font-mono text-ink-muted">mode</code> is always{" "}
          <code className="font-mono text-ink-muted">paper</code> and{" "}
          <code className="font-mono text-ink-muted">bar_interval</code> is always{" "}
          <code className="font-mono text-ink-muted">1d</code>.
        </>
      }
    >
      <form onSubmit={handleSubmit} className="grid gap-3">
        <Field
          label="Paper broker id"
          hint="The UUID of an existing paper broker account. There is no broker picker — copy the id from the brokers admin."
        >
          <input
            className={monoInputClass}
            value={brokerId}
            onChange={(e) => setBrokerId(e.target.value)}
            placeholder="00000000-0000-0000-0000-000000000000"
          />
        </Field>

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
            {loading ? "Creating…" : "Create deployment"}
          </button>
        </div>
      </form>

      {validationError && (
        <Alert tone="warn" testId="deployment-validation">
          {validationError}
        </Alert>
      )}

      {errorDetail && (
        <Alert tone="error">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </Alert>
      )}

      {createdStatus != null && !errorDetail && (
        <Alert tone="warn" role="status" testId="deployment-created">
          Deployment created as <strong>{createdStatus}</strong> — it is NOT
          trading yet. Approve it in the list below to put it on the runner.
          <span className={`mt-1 block ${hintClass}`}>
            Even once approved, nothing runs unless an operator has enabled{" "}
            <code className="font-mono">STRATEGY_RUNNER_ENABLED</code>.
          </span>
        </Alert>
      )}
    </Panel>
  );
}

export default CreateDeploymentForm;
