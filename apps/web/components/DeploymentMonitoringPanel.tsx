"use client";

import { useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import { formatDetail } from "@/components/CreateDeploymentForm";
import {
  Alert,
  EmptyNote,
  Stat,
  TableScroll,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";

/**
 * The shape `GET /deployments/{deploymentId}/monitoring` returns (Phase 65,
 * D083). Every Decimal field serializes as a JSON string, exactly like every
 * other numeric field this API returns (see `StrategyDeploymentRunResponse`
 * / `DeploymentSignalResponse` in `CreateDeploymentForm.tsx`).
 */
export type MonitoringRoundTrip = {
  symbol: string;
  quantity: string;
  entry_price: string;
  entered_at: string;
  exit_price: string;
  exited_at: string;
  realized_pnl: string;
  return_pct: string;
};

export type MonitoringActual = {
  round_trips: MonitoringRoundTrip[];
  /** symbol -> open quantity, as a decimal-as-string. Possibly empty. */
  open_positions: Record<string, string>;
  num_round_trips: number;
  num_winning: number;
  /** `null` (never a fabricated `0`) when `num_round_trips` is 0. */
  win_rate_pct: string | null;
  total_realized_pnl: string;
  /** `null` (never a fabricated `0`) when `num_round_trips` is 0. */
  avg_return_pct: string | null;
};

export type MonitoringExpectedStatus = "available" | "no_reference_backtest";

export type MonitoringExpected = {
  status: MonitoringExpectedStatus;
  /** `null` for every field below when `status` is `"no_reference_backtest"`. */
  reference_backtest_run_id: string | null;
  symbol: string | null;
  total_return_pct: string | null;
  max_drawdown_pct: string | null;
  win_rate_pct: string | null;
  num_trades: number | null;
};

export type DeploymentMonitoringResponse = {
  deployment_id: string;
  as_of: string;
  actual: MonitoringActual;
  expected: MonitoringExpected;
};

/** A rate field is `null` when there isn't enough data to compute it —
 * render an em-dash, never `0%`. */
function orDashPct(raw: string | null): string {
  return raw == null ? "—" : `${raw}%`;
}

/** Show enough of a UUID to spot-check it without wrapping the layout. */
function truncateId(id: string): string {
  return id.length <= 12 ? id : `${id.slice(0, 8)}…`;
}

/**
 * One deployment's "actual vs expected" performance snapshot (Phase 65,
 * D083): its own closed round trips and open positions against the
 * reference backtest for its strategy version. Unlike `DeploymentRunTable`
 * / `DeploymentSignalTable` (dumb tables fed by `DeploymentList`'s own
 * `Promise.all` fetch), this component owns its fetch — `/monitoring` isn't
 * part of that shared runner-history call, and `DeploymentList` mounts this
 * component lazily (only after its `<details>` is first opened), which is
 * what makes the fetch-on-mount below lazy in practice.
 */
export function DeploymentMonitoringPanel({
  deploymentId,
}: {
  deploymentId: string;
}) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<DeploymentMonitoringResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const res = await fetch(`/api/deployments/${deploymentId}/monitoring`, {
          cache: "no-store",
        });
        if (cancelled) return;
        if (handleExpiredSession(res.status)) return;
        const body = (await res.json().catch(() => null)) as
          | (DeploymentMonitoringResponse & { detail?: unknown })
          | null;
        if (!res.ok) {
          setError(
            formatDetail(body?.detail) ?? `Request failed (HTTP ${res.status})`,
          );
          return;
        }
        setData(body);
      } catch {
        if (!cancelled) {
          setError("DATA_UNAVAILABLE: could not reach the trading API");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [deploymentId]);

  if (loading) {
    return <EmptyNote>Loading performance…</EmptyNote>;
  }
  if (error) {
    return <Alert tone="error">{error}</Alert>;
  }
  if (!data) {
    return (
      <Alert tone="error">DATA_UNAVAILABLE: no monitoring data returned</Alert>
    );
  }

  const { actual, expected } = data;
  const openPositions = Object.entries(actual.open_positions);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <p className="text-[11px] font-semibold uppercase tracking-[0.09em] text-ink-faint">
          Expected (from backtest)
        </p>
        {expected.status === "available" ? (
          <div className="flex flex-col gap-2">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Stat
                label="Total return"
                value={orDashPct(expected.total_return_pct)}
                testId="expected-total-return"
              />
              <Stat
                label="Max drawdown"
                value={orDashPct(expected.max_drawdown_pct)}
                testId="expected-max-drawdown"
              />
              <Stat
                label="Win rate"
                value={orDashPct(expected.win_rate_pct)}
                testId="expected-win-rate"
              />
              <Stat
                label="Trades"
                value={expected.num_trades ?? "—"}
                testId="expected-num-trades"
              />
            </div>
            <p className="text-xs text-ink-muted">
              Symbol{" "}
              <span className="font-mono text-ink" data-testid="expected-symbol">
                {expected.symbol ?? "—"}
              </span>
              {" · "}reference backtest{" "}
              <span
                className="font-mono text-[11px] text-ink"
                data-testid="expected-reference-id"
                title={expected.reference_backtest_run_id ?? undefined}
              >
                {expected.reference_backtest_run_id
                  ? truncateId(expected.reference_backtest_run_id)
                  : "—"}
              </span>
            </p>
          </div>
        ) : (
          <EmptyNote>
            No backtest exists yet for this strategy version.
          </EmptyNote>
        )}
      </div>

      <div className="flex flex-col gap-1.5">
        <p className="text-[11px] font-semibold uppercase tracking-[0.09em] text-ink-faint">
          Actual (this deployment&apos;s own trades)
        </p>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat
            label="Round trips"
            value={actual.num_round_trips}
            testId="actual-num-round-trips"
          />
          <Stat
            label="Win rate"
            value={orDashPct(actual.win_rate_pct)}
            testId="actual-win-rate"
          />
          <Stat
            label="Realized PnL"
            value={actual.total_realized_pnl}
            testId="actual-realized-pnl"
          />
          <Stat
            label="Avg return"
            value={orDashPct(actual.avg_return_pct)}
            testId="actual-avg-return"
          />
        </div>

        {openPositions.length > 0 && (
          <ul
            className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-muted"
            data-testid="actual-open-positions"
          >
            {openPositions.map(([symbol, qty]) => (
              <li key={symbol} className="font-mono">
                {symbol}: {qty} shares open
              </li>
            ))}
          </ul>
        )}

        <TableScroll>
          <table className={tableClass}>
            <caption className="sr-only">
              This deployment&apos;s closed round trips, exactly as returned
              by the API
            </caption>
            <thead>
              <tr className={theadRowClass}>
                <th className={thClass}>Symbol</th>
                <th className={thClass}>Quantity</th>
                <th className={thClass}>Entry</th>
                <th className={thClass}>Exit</th>
                <th className={thClass}>Realized PnL</th>
                <th className={thClass}>Return</th>
              </tr>
            </thead>
            <tbody>
              {actual.round_trips.length === 0 ? (
                <tr className={tbodyRowClass}>
                  <td
                    className={`${tdClass} font-sans text-ink-muted`}
                    colSpan={6}
                    data-testid="actual-round-trips-empty"
                  >
                    no closed trades yet
                  </td>
                </tr>
              ) : (
                actual.round_trips.map((rt, i) => (
                  <tr
                    key={`${rt.symbol}-${rt.entered_at}-${i}`}
                    className={tbodyRowClass}
                    data-testid="deployment-round-trip-row"
                  >
                    <td className={`${tdClass} font-semibold`}>{rt.symbol}</td>
                    <td className={tdClass}>{rt.quantity}</td>
                    <td className={tdClass}>
                      {rt.entry_price}{" "}
                      <span className="text-ink-muted">@ {rt.entered_at}</span>
                    </td>
                    <td className={tdClass}>
                      {rt.exit_price}{" "}
                      <span className="text-ink-muted">@ {rt.exited_at}</span>
                    </td>
                    <td className={tdClass}>{rt.realized_pnl}</td>
                    <td className={tdClass}>{rt.return_pct}%</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </TableScroll>
      </div>
    </div>
  );
}

export default DeploymentMonitoringPanel;
