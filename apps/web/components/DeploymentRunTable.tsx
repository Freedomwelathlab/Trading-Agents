import {
  EmptyNote,
  Pill,
  TableScroll,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";
import type {
  DeploymentRunStatus,
  StrategyDeploymentRunResponse,
} from "@/components/CreateDeploymentForm";

/** succeeded -> pos, failed -> neg, every `skipped_*` -> neutral. A skipped
 * cycle is a real, honest answer ("nothing to do"), never an error.
 *
 * One deliberate exception (Phase 69, D087): `skipped_live_risk_halt` is
 * styled `neg`. It is technically a skip, but it means a capital circuit
 * breaker fired on REAL money and paused the deployment — the one skip an
 * operator must not scan past as routine. */
function runTone(status: DeploymentRunStatus): "pos" | "neg" | "neutral" {
  if (status === "succeeded") return "pos";
  if (status === "failed") return "neg";
  if (status === "skipped_live_risk_halt") return "neg";
  return "neutral";
}

/** A timestamp is `null` while a run is still in flight — render an em-dash. */
function orDash(raw: string | null): string {
  return raw == null ? "—" : raw;
}

/**
 * One deployment's run history (Phase 63) — the append-only
 * `strategy_deployment_runs` trail, newest first, exactly as the backend
 * returned it. NOT a client component (no hooks), matching `SignalTable` /
 * `BacktestTradeLedger`: `runs` is supplied by the parent.
 *
 * `error_detail` is rendered verbatim, in full — a failed cycle (e.g. a
 * held position with no bar to mark against) names exactly what went
 * wrong, and the master spec forbids hiding it.
 */
export function DeploymentRunTable({
  runs,
}: {
  runs: StrategyDeploymentRunResponse[];
}) {
  if (runs.length === 0) {
    return (
      <EmptyNote>
        No runner cycles for this deployment yet — the runner is off unless an
        operator has enabled STRATEGY_RUNNER_ENABLED, and it only touches an
        approved (active) deployment.
      </EmptyNote>
    );
  }
  return (
    <TableScroll>
      <table className={tableClass}>
        <caption className="sr-only">
          Runner cycles for this deployment, exactly as returned by the API
        </caption>
        <thead>
          <tr className={theadRowClass}>
            <th className={thClass}>Status</th>
            <th className={thClass}>Started</th>
            <th className={thClass}>Completed</th>
            <th className={thClass}>Evaluated</th>
            <th className={thClass}>Actionable</th>
            <th className={thClass}>Submitted</th>
            <th className={thClass}>Filled</th>
            <th className={`${thClass} w-full`}>Error detail</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr
              key={run.id}
              className={tbodyRowClass}
              data-testid="deployment-run-row"
            >
              <td className={tdClass}>
                <Pill tone={runTone(run.status)} testId={`run-status-${run.id}`}>
                  {run.status}
                </Pill>
              </td>
              <td className={`${tdClass} text-ink-muted`}>{run.started_at}</td>
              <td className={`${tdClass} text-ink-muted`}>
                {orDash(run.completed_at)}
              </td>
              <td className={tdClass} data-testid={`run-evaluated-${run.id}`}>
                {run.symbols_evaluated}
              </td>
              <td className={tdClass} data-testid={`run-actionable-${run.id}`}>
                {run.signals_actionable}
              </td>
              <td className={tdClass} data-testid={`run-submitted-${run.id}`}>
                {run.orders_submitted}
              </td>
              <td className={tdClass} data-testid={`run-filled-${run.id}`}>
                {run.orders_filled}
              </td>
              <td
                className={`${tdClass} whitespace-normal font-sans text-ink`}
                data-testid={`run-error-${run.id}`}
              >
                {run.error_detail ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableScroll>
  );
}

export default DeploymentRunTable;
