"use client";

import { Fragment, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  EmptyNote,
  Panel,
  Pill,
  TableScroll,
  btnGhost,
  btnSecondary,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";
import {
  formatDetail,
  type DeploymentStatus,
  type StrategyDeploymentResponse,
  type StrategyDeploymentRunResponse,
  type DeploymentSignalResponse,
  type DriftCheckResponse,
  type ListDeploymentRunsResponse,
  type ListDeploymentSignalsResponse,
  type ListDriftChecksResponse,
} from "@/components/CreateDeploymentForm";
import { DeploymentRunTable } from "@/components/DeploymentRunTable";
import { DeploymentSignalTable } from "@/components/DeploymentSignalTable";
import { DeploymentMonitoringPanel } from "@/components/DeploymentMonitoringPanel";
import { DeploymentDriftTable } from "@/components/DeploymentDriftTable";

const HISTORY_LIMIT = 50;

type LifecycleAction = "approve" | "pause" | "resume" | "stop";

/** pending_approval -> warn (amber), active -> pos, paused -> neutral,
 * stopped -> neg. Every tone is derived from real backend data. */
function statusTone(status: DeploymentStatus): "warn" | "pos" | "neutral" | "neg" {
  if (status === "active") return "pos";
  if (status === "pending_approval") return "warn";
  if (status === "stopped") return "neg";
  return "neutral";
}

/**
 * A version's deployment history (Phase 63, D081). `deployments` is
 * supplied by the parent page, which owns that fetch; this component owns
 * the lifecycle actions and the per-row run / signal history it loads on
 * expansion.
 *
 * Each row shows the status as a `Pill`, the symbols, the created and
 * approved times, and the lifecycle buttons the current status allows:
 * Approve only for `pending_approval`, Pause for `active`, Resume for
 * `paused`, Stop for anything not already `stopped`. Approve hits the
 * endpoint gated by the separate, stricter `strategy:approve_deployment`
 * permission — a 403 there renders an inline explanation rather than a
 * generic error. A 409 shows the backend's plain-string `detail` verbatim.
 */
export function DeploymentList({
  deployments,
  onChanged,
}: {
  deployments: StrategyDeploymentResponse[];
  onChanged: () => void;
}) {
  return (
    <Panel
      title="Deployments"
      description="Every deployment for this version, newest first, exactly as the backend returned it. Expand a row for its runner cycles and the signals they produced."
    >
      {deployments.length === 0 ? (
        <EmptyNote>
          No deployments yet for this version — create one above. A new
          deployment is <code className="font-mono">pending_approval</code> and
          does not trade until it is approved.
        </EmptyNote>
      ) : (
        <TableScroll>
          <table className={tableClass}>
            <caption className="sr-only">
              Deployments for this version, exactly as returned by the API
            </caption>
            <thead>
              <tr className={theadRowClass}>
                <th className={thClass}>Status</th>
                <th className={`${thClass} w-full`}>Symbols</th>
                <th className={thClass}>Created</th>
                <th className={thClass}>Approved</th>
                <th className={thClass}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {deployments.map((d) => (
                <DeploymentRow key={d.id} deployment={d} onChanged={onChanged} />
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}
    </Panel>
  );
}

function DeploymentRow({
  deployment: d,
  onChanged,
}: {
  deployment: StrategyDeploymentResponse;
  onChanged: () => void;
}) {
  const [actionLoading, setActionLoading] = useState<LifecycleAction | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [permissionHint, setPermissionHint] = useState(false);

  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [runs, setRuns] = useState<StrategyDeploymentRunResponse[]>([]);
  const [signals, setSignals] = useState<DeploymentSignalResponse[]>([]);

  const [performanceOpened, setPerformanceOpened] = useState(false);

  const [driftLoaded, setDriftLoaded] = useState(false);
  const [driftLoading, setDriftLoading] = useState(false);
  const [driftError, setDriftError] = useState<string | null>(null);
  const [driftChecks, setDriftChecks] = useState<DriftCheckResponse[]>([]);

  async function act(action: LifecycleAction) {
    setActionError(null);
    setPermissionHint(false);
    setActionLoading(action);
    try {
      const withBody = action === "approve" || action === "pause";
      const res = await fetch(`/api/deployments/${d.id}/${action}`, {
        method: "POST",
        ...(withBody
          ? { headers: { "Content-Type": "application/json" }, body: "{}" }
          : {}),
      });
      const data = (await res.json().catch(() => null)) as
        | { detail?: unknown }
        | null;
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        if (action === "approve" && res.status === 403) {
          setPermissionHint(true);
          return;
        }
        setActionError(
          formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`,
        );
        return;
      }
      onChanged();
    } catch {
      setActionError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setActionLoading(null);
    }
  }

  async function loadHistory() {
    if (historyLoaded || historyLoading) return;
    setHistoryLoading(true);
    setHistoryError(null);
    try {
      const [runsRes, sigRes] = await Promise.all([
        fetch(`/api/deployments/${d.id}/runs?limit=${HISTORY_LIMIT}&offset=0`, {
          cache: "no-store",
        }),
        fetch(`/api/deployments/${d.id}/signals?limit=${HISTORY_LIMIT}&offset=0`, {
          cache: "no-store",
        }),
      ]);
      if (handleExpiredSession(runsRes.status) || handleExpiredSession(sigRes.status)) {
        return;
      }
      const runsData = (await runsRes.json().catch(() => null)) as
        | (ListDeploymentRunsResponse & { detail?: unknown })
        | null;
      const sigData = (await sigRes.json().catch(() => null)) as
        | (ListDeploymentSignalsResponse & { detail?: unknown })
        | null;
      if (!runsRes.ok) {
        setHistoryError(
          formatDetail(runsData?.detail) ??
            `Request failed (HTTP ${runsRes.status})`,
        );
        return;
      }
      if (!sigRes.ok) {
        setHistoryError(
          formatDetail(sigData?.detail) ??
            `Request failed (HTTP ${sigRes.status})`,
        );
        return;
      }
      setRuns(runsData?.items ?? []);
      setSignals(sigData?.items ?? []);
      setHistoryLoaded(true);
    } catch {
      setHistoryError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setHistoryLoading(false);
    }
  }

  async function loadDrift() {
    if (driftLoaded || driftLoading) return;
    setDriftLoading(true);
    setDriftError(null);
    try {
      const res = await fetch(
        `/api/deployments/${d.id}/drift-checks?limit=${HISTORY_LIMIT}&offset=0`,
        { cache: "no-store" },
      );
      if (handleExpiredSession(res.status)) return;
      const data = (await res.json().catch(() => null)) as
        | (ListDriftChecksResponse & { detail?: unknown })
        | null;
      if (!res.ok) {
        setDriftError(
          formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`,
        );
        return;
      }
      setDriftChecks(data?.items ?? []);
      setDriftLoaded(true);
    } catch {
      setDriftError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setDriftLoading(false);
    }
  }

  const busy = actionLoading != null;

  return (
    <Fragment>
      <tr className={tbodyRowClass} data-testid="deployment-row">
        <td className={tdClass}>
          <Pill tone={statusTone(d.status)} testId={`deployment-status-${d.id}`}>
            {d.status}
          </Pill>
        </td>
        <td className={`${tdClass} whitespace-normal font-sans text-ink`}>
          {d.symbols.join(", ")}
        </td>
        <td className={`${tdClass} text-ink-muted`}>{d.created_at}</td>
        <td className={`${tdClass} text-ink-muted`}>{d.approved_at ?? "—"}</td>
        <td className={tdClass}>
          <span className="flex flex-wrap gap-1.5">
            {d.status === "pending_approval" && (
              <button
                type="button"
                className={btnSecondary}
                disabled={busy}
                onClick={() => act("approve")}
                data-testid={`approve-${d.id}`}
              >
                {actionLoading === "approve" ? "Approving…" : "Approve"}
              </button>
            )}
            {d.status === "active" && (
              <button
                type="button"
                className={btnSecondary}
                disabled={busy}
                onClick={() => act("pause")}
                data-testid={`pause-${d.id}`}
              >
                {actionLoading === "pause" ? "Pausing…" : "Pause"}
              </button>
            )}
            {d.status === "paused" && (
              <button
                type="button"
                className={btnSecondary}
                disabled={busy}
                onClick={() => act("resume")}
                data-testid={`resume-${d.id}`}
              >
                {actionLoading === "resume" ? "Resuming…" : "Resume"}
              </button>
            )}
            {d.status !== "stopped" && (
              <button
                type="button"
                className={btnGhost}
                disabled={busy}
                onClick={() => act("stop")}
                data-testid={`stop-${d.id}`}
              >
                {actionLoading === "stop" ? "Stopping…" : "Stop"}
              </button>
            )}
          </span>
        </td>
      </tr>

      {(permissionHint || actionError) && (
        <tr className={tbodyRowClass}>
          <td className={`${tdClass} whitespace-normal`} colSpan={5}>
            {permissionHint && (
              <Alert tone="warn" testId={`approve-permission-hint-${d.id}`}>
                Approving a deployment needs the separate{" "}
                <code className="font-mono">strategy:approve_deployment</code>{" "}
                permission — deliberately stricter than{" "}
                <code className="font-mono">strategy:deploy</code> so an
                organisation can require a second person. Your account does not
                hold it.
              </Alert>
            )}
            {actionError && (
              <Alert tone="error" testId={`deployment-action-error-${d.id}`}>
                {actionError}
              </Alert>
            )}
          </td>
        </tr>
      )}

      <tr className={tbodyRowClass} data-testid={`deployment-detail-row-${d.id}`}>
        <td className={`${tdClass} whitespace-normal`} colSpan={5}>
          <details
            data-testid={`deployment-details-${d.id}`}
            onToggle={(e) => {
              if ((e.currentTarget as HTMLDetailsElement).open) void loadHistory();
            }}
          >
            <summary
              className={`${btnGhost} list-none [&::-webkit-details-marker]:hidden`}
              data-testid={`deployment-toggle-${d.id}`}
            >
              Runner cycles &amp; signals
            </summary>
            <div className="mt-3 flex flex-col gap-3">
              <dl className="grid grid-cols-[auto_1fr] items-baseline gap-x-4 gap-y-1 text-xs">
                <dt className="font-mono text-[11px] uppercase tracking-wide text-ink-faint">
                  broker_id
                </dt>
                <dd className="tnum font-mono text-[13px] text-ink">
                  {d.broker_id}
                </dd>
                <dt className="font-mono text-[11px] uppercase tracking-wide text-ink-faint">
                  mode
                </dt>
                <dd className="font-mono text-[13px] text-ink">{d.mode}</dd>
                <dt className="font-mono text-[11px] uppercase tracking-wide text-ink-faint">
                  last_evaluated_at
                </dt>
                <dd
                  className="font-mono text-[13px] text-ink"
                  data-testid={`deployment-last-evaluated-${d.id}`}
                >
                  {d.last_evaluated_at ?? "—"}
                </dd>
                {d.status === "paused" && (
                  <>
                    <dt className="font-mono text-[11px] uppercase tracking-wide text-ink-faint">
                      paused_reason
                    </dt>
                    <dd className="font-sans text-[13px] text-ink">
                      {d.paused_reason ?? "—"}
                    </dd>
                  </>
                )}
              </dl>

              {historyLoading && !historyLoaded && (
                <EmptyNote>Loading runner history…</EmptyNote>
              )}
              {historyError && <Alert tone="error">{historyError}</Alert>}
              {historyLoaded && !historyError && (
                <>
                  <div className="flex flex-col gap-1.5">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.09em] text-ink-faint">
                      Runner cycles
                    </p>
                    <DeploymentRunTable runs={runs} />
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.09em] text-ink-faint">
                      Signals
                    </p>
                    <DeploymentSignalTable signals={signals} />
                  </div>
                </>
              )}
            </div>
          </details>
        </td>
      </tr>

      <tr className={tbodyRowClass} data-testid={`deployment-performance-row-${d.id}`}>
        <td className={`${tdClass} whitespace-normal`} colSpan={5}>
          <details
            data-testid={`deployment-performance-details-${d.id}`}
            onToggle={(e) => {
              if ((e.currentTarget as HTMLDetailsElement).open) {
                setPerformanceOpened(true);
              }
            }}
          >
            <summary
              className={`${btnGhost} list-none [&::-webkit-details-marker]:hidden`}
              data-testid={`deployment-performance-toggle-${d.id}`}
            >
              Performance
            </summary>
            <div className="mt-3">
              {performanceOpened && (
                <DeploymentMonitoringPanel deploymentId={d.id} />
              )}
            </div>
          </details>
        </td>
      </tr>

      <tr className={tbodyRowClass} data-testid={`deployment-drift-row-${d.id}`}>
        <td className={`${tdClass} whitespace-normal`} colSpan={5}>
          <details
            data-testid={`deployment-drift-details-${d.id}`}
            onToggle={(e) => {
              if ((e.currentTarget as HTMLDetailsElement).open) void loadDrift();
            }}
          >
            <summary
              className={`${btnGhost} list-none [&::-webkit-details-marker]:hidden`}
              data-testid={`deployment-drift-toggle-${d.id}`}
            >
              Drift checks
            </summary>
            <div className="mt-3 flex flex-col gap-3">
              {driftLoading && !driftLoaded && (
                <EmptyNote>Loading drift checks…</EmptyNote>
              )}
              {driftError && (
                <Alert tone="error" testId={`deployment-drift-error-${d.id}`}>
                  {driftError}
                </Alert>
              )}
              {driftLoaded && !driftError && (
                <DeploymentDriftTable driftChecks={driftChecks} />
              )}
            </div>
          </details>
        </td>
      </tr>
    </Fragment>
  );
}

export default DeploymentList;
