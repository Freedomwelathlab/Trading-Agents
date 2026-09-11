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
  DriftCheckActionTaken,
  DriftCheckResponse,
  DriftCheckStatus,
} from "@/components/CreateDeploymentForm";

/** `no_drift` -> pos, `drift_detected` -> warn (needs attention, not
 * necessarily an error — the runner may only be observing), `insufficient_data`
 * -> neutral (a real, honest "not enough data yet" answer). */
function driftStatusTone(status: DriftCheckStatus): "pos" | "warn" | "neutral" {
  if (status === "no_drift") return "pos";
  if (status === "drift_detected") return "warn";
  return "neutral";
}

/** `paused` is the one `action_taken` value where the runner actually
 * changed the deployment's state on its own — give it a visually distinct
 * (red) tone. `none` and `observed_only` are both purely informational and
 * share the same neutral tone. */
function actionTone(action: DriftCheckActionTaken): "neg" | "neutral" {
  return action === "paused" ? "neg" : "neutral";
}

/** The three win-rate fields are `null` together, only when
 * `status === "insufficient_data"` — render an em-dash, never `0%`. */
function orDashPct(raw: string | null): string {
  return raw == null ? "—" : `${raw}%`;
}

/**
 * One deployment's drift-check history (Phase 66, D084) — every
 * `strategy_drift_checks` row a completed runner cycle produced, newest
 * first, exactly as the backend returned it. NOT a client component (no
 * hooks), matching `DeploymentRunTable` / `DeploymentSignalTable`:
 * `driftChecks` is supplied by the parent's own fetch.
 *
 * `detail` is rendered verbatim, in full, allowed to wrap — it is the
 * human-readable line the backend always provides regardless of `status`,
 * including for `insufficient_data` rows where the three win-rate fields
 * are `null`.
 */
export function DeploymentDriftTable({
  driftChecks,
}: {
  driftChecks: DriftCheckResponse[];
}) {
  if (driftChecks.length === 0) {
    return (
      <EmptyNote>
        No drift checks yet — no successful runner cycle has completed one
        for this deployment.
      </EmptyNote>
    );
  }
  return (
    <TableScroll>
      <table className={tableClass}>
        <caption className="sr-only">
          Drift checks for this deployment, exactly as returned by the API
        </caption>
        <thead>
          <tr className={theadRowClass}>
            <th className={thClass}>Created</th>
            <th className={thClass}>Status</th>
            <th className={thClass}>Actual win rate</th>
            <th className={thClass}>Expected win rate</th>
            <th className={thClass}>Deviation</th>
            <th className={thClass}>Round trips</th>
            <th className={thClass}>Action taken</th>
            <th className={`${thClass} w-full`}>Detail</th>
          </tr>
        </thead>
        <tbody>
          {driftChecks.map((dc) => (
            <tr
              key={dc.id}
              className={tbodyRowClass}
              data-testid="drift-check-row"
            >
              <td className={`${tdClass} text-ink-muted`}>{dc.created_at}</td>
              <td className={tdClass}>
                <Pill
                  tone={driftStatusTone(dc.status)}
                  testId={`drift-status-${dc.id}`}
                >
                  {dc.status}
                </Pill>
              </td>
              <td
                className={tdClass}
                data-testid={`drift-actual-win-rate-${dc.id}`}
              >
                {orDashPct(dc.actual_win_rate_pct)}
              </td>
              <td
                className={tdClass}
                data-testid={`drift-expected-win-rate-${dc.id}`}
              >
                {orDashPct(dc.expected_win_rate_pct)}
              </td>
              <td className={tdClass} data-testid={`drift-deviation-${dc.id}`}>
                {orDashPct(dc.win_rate_deviation_pct)}
              </td>
              <td
                className={tdClass}
                data-testid={`drift-round-trips-${dc.id}`}
              >
                {dc.num_round_trips}
              </td>
              <td className={tdClass}>
                <Pill
                  tone={actionTone(dc.action_taken)}
                  testId={`drift-action-${dc.id}`}
                >
                  {dc.action_taken}
                </Pill>
              </td>
              <td
                className={`${tdClass} whitespace-normal font-sans text-ink`}
                data-testid={`drift-detail-${dc.id}`}
              >
                {dc.detail}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableScroll>
  );
}

export default DeploymentDriftTable;
