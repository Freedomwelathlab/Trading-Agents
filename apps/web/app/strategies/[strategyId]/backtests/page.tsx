"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { handleExpiredSession } from "@/lib/session";
import AppShell from "@/components/shell/AppShell";
import { RunBacktestForm, type BacktestRunSummary } from "@/components/RunBacktestForm";
import { BacktestRunList } from "@/components/BacktestRunList";
import { BacktestRunComparison } from "@/components/BacktestRunComparison";
import {
  Alert,
  EmptyNote,
  Field,
  Panel,
  SectionHeading,
  inputClass,
} from "@/components/ui/primitives";

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

type ListBacktestRunsResponse = {
  items?: BacktestRunSummary[];
  limit?: number;
  offset?: number;
};

/** Mirrors `BacktestPanel.formatDetail` / `RunBacktestForm.formatDetail`. */
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
 * Backtest runs for one strategy: trigger a run, browse the version's run
 * history, and compare several runs side by side (Phase 56).
 *
 * Entirely a client component, same shape as
 * `app/strategies/[strategyId]/page.tsx` (Phase 54) — the version
 * selector, the trigger form and the run list all share state that only
 * makes sense wired together here, and no separate "detail" sub-component
 * is in this phase's scope.
 *
 * Version-selector UX: every version is listed (not just validated ones),
 * so the reader can see the whole history and why an option is
 * unavailable, rather than a silently-shortened list. A version whose
 * `status` is not `"validated"` is disabled in the `<select>` and its
 * label says so, because the backend 409s `VERSION_NOT_VALIDATED` for
 * anything else (schemas_strategy_backtests.py) — this warns before the
 * request is even made, rather than only after a rejection.
 */
export default function StrategyBacktestsPage() {
  const params = useParams<{ strategyId: string }>();
  const strategyId = params.strategyId;

  const [strategyName, setStrategyName] = useState<string | null>(null);
  const [versions, setVersions] = useState<StrategyVersionSummary[] | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [loadingStrategy, setLoadingStrategy] = useState(false);
  const [strategyError, setStrategyError] = useState<string | null>(null);

  const [runs, setRuns] = useState<BacktestRunSummary[] | null>(null);
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [runsStatus, setRunsStatus] = useState<number | null>(null);

  const [selectedRunIds, setSelectedRunIds] = useState<string[]>([]);

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
          setStrategyError(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
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
    // Load once per strategyId, matching `app/strategies/[strategyId]/page.tsx`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyId]);

  async function loadRuns(versionId: string) {
    setLoadingRuns(true);
    setRunsError(null);
    setRunsStatus(null);
    try {
      const res = await fetch(
        `/api/strategies/${strategyId}/versions/${versionId}/backtests?limit=50&offset=0`,
        { cache: "no-store" },
      );
      const data = (await res.json().catch(() => null)) as
        | (ListBacktestRunsResponse & { detail?: unknown })
        | null;
      setRunsStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setRuns(null);
        setRunsError(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setRuns(data?.items ?? []);
    } catch {
      setRuns(null);
      setRunsError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoadingRuns(false);
    }
  }

  useEffect(() => {
    if (selectedVersionId) void loadRuns(selectedVersionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedVersionId]);

  const selectedVersion = versions?.find((v) => v.id === selectedVersionId) ?? null;

  return (
    <AppShell
      title={strategyName ? `${strategyName} — Backtests` : "Backtests"}
      subtitle="Trigger a persisted backtest run for a strategy version, then browse and compare its run history."
    >
      <div className="flex flex-col gap-5">
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
            description="Only a validated version can be backtested — an unvalidated one is disabled below because the backend rejects a run against it (409 VERSION_NOT_VALIDATED)."
          >
            <Field
              label="Strategy version"
              hint={
                selectedVersion && selectedVersion.status !== "validated"
                  ? `v${selectedVersion.version_number} is "${selectedVersion.status}" — validate it in the strategy builder before running a backtest.`
                  : undefined
              }
            >
              <select
                className={inputClass}
                value={selectedVersionId ?? ""}
                onChange={(e) => {
                  setSelectedVersionId(e.target.value);
                  setSelectedRunIds([]);
                }}
              >
                {versions.map((v) => (
                  <option key={v.id} value={v.id} disabled={v.status !== "validated"}>
                    v{v.version_number} — {v.status}
                    {v.status !== "validated" ? " (not backtestable)" : ""}
                  </option>
                ))}
              </select>
            </Field>
          </Panel>
        )}

        {selectedVersionId && (
          <RunBacktestForm
            strategyId={strategyId}
            versionId={selectedVersionId}
            onRunComplete={() => void loadRuns(selectedVersionId)}
          />
        )}

        {runsError && (
          <Alert tone="error">
            {runsStatus ? `HTTP ${runsStatus}: ` : ""}
            {runsError}
          </Alert>
        )}

        {loadingRuns && !runs && !runsError && <EmptyNote>Loading backtest runs…</EmptyNote>}

        {selectedVersionId && runs && !runsError && (
          <BacktestRunList
            strategyId={strategyId}
            versionId={selectedVersionId}
            runs={runs}
            onSelectionChange={setSelectedRunIds}
          />
        )}

        {selectedVersionId && (
          <>
            <SectionHeading note={`${selectedRunIds.length} selected`}>
              Compare runs
            </SectionHeading>
            <BacktestRunComparison
              strategyId={strategyId}
              versionId={selectedVersionId}
              selectedRunIds={selectedRunIds}
            />
          </>
        )}
      </div>
    </AppShell>
  );
}
