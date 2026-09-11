"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { handleExpiredSession } from "@/lib/session";
import AppShell from "@/components/shell/AppShell";
import {
  CreateDeploymentForm,
  formatDetail,
  type ListDeploymentsResponse,
  type StrategyDeploymentResponse,
} from "@/components/CreateDeploymentForm";
import { DeploymentList } from "@/components/DeploymentList";
import { Alert, EmptyNote, Field, Panel, inputClass } from "@/components/ui/primitives";

type StrategyVersionSummary = {
  id: string;
  version_number: number;
  status: "draft" | "validated" | "archived";
  created_at: string;
  validated_at: string | null;
};

type StrategyDetailResponse = {
  id: string;
  name: string;
  latest_version: { id: string };
  versions: StrategyVersionSummary[];
};

const HISTORY_LIMIT = 50;

/**
 * Paper-trading deployments for one strategy: put a validated version on
 * the scheduled runner (behind a mandatory approval gate), then manage and
 * inspect that version's deployments (Phase 63, D081).
 *
 * Same shape as `app/strategies/[strategyId]/signals/page.tsx` — a fully
 * client component whose version selector, create form and deployment list
 * share state that only makes sense wired together here. The version
 * selector lists every version so the reader sees the whole history and
 * why an option is unavailable; a version whose `status` is not
 * `"validated"` is disabled, because the backend 409s
 * `VERSION_NOT_VALIDATED` for anything else.
 *
 * There is no `SideNav` entry — like backtests, universe scans and
 * signals, deployments live under a strategy and are reached by URL.
 */
export default function StrategyDeploymentsPage() {
  const params = useParams<{ strategyId: string }>();
  const strategyId = params.strategyId;

  const [strategyName, setStrategyName] = useState<string | null>(null);
  const [versions, setVersions] = useState<StrategyVersionSummary[] | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [loadingStrategy, setLoadingStrategy] = useState(false);
  const [strategyError, setStrategyError] = useState<string | null>(null);

  const [deployments, setDeployments] = useState<
    StrategyDeploymentResponse[] | null
  >(null);
  const [loadingDeployments, setLoadingDeployments] = useState(false);
  const [deploymentsError, setDeploymentsError] = useState<string | null>(null);
  const [deploymentsStatus, setDeploymentsStatus] = useState<number | null>(null);

  useEffect(() => {
    void (async () => {
      setLoadingStrategy(true);
      setStrategyError(null);
      try {
        const res = await fetch(`/api/strategies/${strategyId}`, { cache: "no-store" });
        const data = (await res.json().catch(() => null)) as
          | (StrategyDetailResponse & { detail?: unknown })
          | null;
        if (!res.ok) {
          if (handleExpiredSession(res.status)) return;
          setStrategyError(
            formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`,
          );
          return;
        }
        setStrategyName(data?.name ?? null);
        const vs = data?.versions ?? [];
        setVersions(vs);
        setSelectedVersionId(data?.latest_version?.id ?? vs[0]?.id ?? null);
      } catch {
        setStrategyError("DATA_UNAVAILABLE: could not reach the trading API");
      } finally {
        setLoadingStrategy(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyId]);

  async function loadDeployments(versionId: string) {
    setLoadingDeployments(true);
    setDeploymentsError(null);
    setDeploymentsStatus(null);
    try {
      const res = await fetch(
        `/api/strategies/${strategyId}/versions/${versionId}/deployments?limit=${HISTORY_LIMIT}&offset=0`,
        { cache: "no-store" },
      );
      const data = (await res.json().catch(() => null)) as
        | (ListDeploymentsResponse & { detail?: unknown })
        | null;
      setDeploymentsStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setDeployments(null);
        setDeploymentsError(
          formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`,
        );
        return;
      }
      setDeployments(data?.items ?? []);
    } catch {
      setDeployments(null);
      setDeploymentsError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoadingDeployments(false);
    }
  }

  useEffect(() => {
    if (selectedVersionId) void loadDeployments(selectedVersionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedVersionId]);

  const selectedVersion = versions?.find((v) => v.id === selectedVersionId) ?? null;

  return (
    <AppShell
      title={strategyName ? `${strategyName} — Deployments` : "Deployments"}
      subtitle="Put a validated strategy version on the scheduled paper-trading runner, behind a mandatory human-approval gate, then manage and inspect its deployments."
    >
      <div className="flex flex-col gap-5">
        <Alert tone="warn" role="status" testId="deployments-safety-note">
          These deployments place <strong>paper</strong> trades only. The
          scheduled runner is OFF unless an operator has set{" "}
          <code className="font-mono">STRATEGY_RUNNER_ENABLED</code>, and a
          deployment does nothing — no evaluation, no order — until it is
          explicitly approved. The global emergency stop halts every runner
          cycle.
        </Alert>

        {strategyError && <Alert tone="error">{strategyError}</Alert>}

        {loadingStrategy && !versions && !strategyError && (
          <EmptyNote>Loading strategy…</EmptyNote>
        )}

        {versions && versions.length === 0 && !strategyError && (
          <EmptyNote>This strategy has no versions yet.</EmptyNote>
        )}

        {versions && versions.length > 0 && (
          <Panel
            title="Version"
            description="Only a validated version can be deployed — an unvalidated one is disabled below because the backend rejects a deployment against it (409 VERSION_NOT_VALIDATED)."
          >
            <Field
              label="Strategy version"
              hint={
                selectedVersion && selectedVersion.status !== "validated"
                  ? `v${selectedVersion.version_number} is "${selectedVersion.status}" — validate it in the strategy builder before deploying.`
                  : undefined
              }
            >
              <select
                className={inputClass}
                value={selectedVersionId ?? ""}
                onChange={(e) => setSelectedVersionId(e.target.value)}
              >
                {versions.map((v) => (
                  <option key={v.id} value={v.id} disabled={v.status !== "validated"}>
                    v{v.version_number} — {v.status}
                    {v.status !== "validated" ? " (not deployable)" : ""}
                  </option>
                ))}
              </select>
            </Field>
          </Panel>
        )}

        {selectedVersionId && (
          <CreateDeploymentForm
            strategyId={strategyId}
            versionId={selectedVersionId}
            onCreated={() => void loadDeployments(selectedVersionId)}
          />
        )}

        {deploymentsError && (
          <Alert tone="error">
            {deploymentsStatus ? `HTTP ${deploymentsStatus}: ` : ""}
            {deploymentsError}
          </Alert>
        )}

        {loadingDeployments && !deployments && !deploymentsError && (
          <EmptyNote>Loading deployments…</EmptyNote>
        )}

        {selectedVersionId && deployments && !deploymentsError && (
          <DeploymentList
            deployments={deployments}
            onChanged={() => void loadDeployments(selectedVersionId)}
          />
        )}
      </div>
    </AppShell>
  );
}
