"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { handleExpiredSession } from "@/lib/session";
import AppShell from "@/components/shell/AppShell";
import {
  CheckSignalsForm,
  formatDetail,
  type ListSignalEvaluationsResponse,
  type SignalEvaluationResponse,
} from "@/components/CheckSignalsForm";
import { SignalTable } from "@/components/SignalTable";
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
 * Signals for one strategy: ask whether a validated version currently says
 * BUY / SELL / HOLD for a batch of symbols — always with a plain-language
 * reason — then browse that version's most recent evaluations (Phase 61).
 *
 * Same shape as `app/strategies/[strategyId]/universe-scans/page.tsx` — a
 * fully client component whose version selector, check-signals form and
 * signal table share state that only makes sense wired together here. The
 * version selector lists every version (not just validated ones) so the
 * reader sees the whole history and why an option is unavailable; a version
 * whose `status` is not `"validated"` is disabled, because the backend 409s
 * `VERSION_NOT_VALIDATED` for anything else.
 */
export default function StrategySignalsPage() {
  const params = useParams<{ strategyId: string }>();
  const strategyId = params.strategyId;

  const [strategyName, setStrategyName] = useState<string | null>(null);
  const [versions, setVersions] = useState<StrategyVersionSummary[] | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [loadingStrategy, setLoadingStrategy] = useState(false);
  const [strategyError, setStrategyError] = useState<string | null>(null);

  const [evaluations, setEvaluations] = useState<SignalEvaluationResponse[] | null>(
    null,
  );
  const [loadingEvaluations, setLoadingEvaluations] = useState(false);
  const [evaluationsError, setEvaluationsError] = useState<string | null>(null);
  const [evaluationsStatus, setEvaluationsStatus] = useState<number | null>(null);

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

  async function loadEvaluations(versionId: string) {
    setLoadingEvaluations(true);
    setEvaluationsError(null);
    setEvaluationsStatus(null);
    try {
      const res = await fetch(
        `/api/strategies/${strategyId}/versions/${versionId}/signals?limit=${HISTORY_LIMIT}&offset=0`,
        { cache: "no-store" },
      );
      const data = (await res.json().catch(() => null)) as
        | (ListSignalEvaluationsResponse & { detail?: unknown })
        | null;
      setEvaluationsStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setEvaluations(null);
        setEvaluationsError(
          formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`,
        );
        return;
      }
      setEvaluations(data?.items ?? []);
    } catch {
      setEvaluations(null);
      setEvaluationsError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoadingEvaluations(false);
    }
  }

  useEffect(() => {
    if (!selectedVersionId) return;
    // Deferred by one microtask so the loader's first `setState` lands
    // AFTER this effect returns rather than during it - calling it
    // synchronously re-renders from inside the effect React is still
    // committing, which `react-hooks` flags as a cascading render. The id
    // is captured so a fast version switch cannot apply the wrong one.
    const versionId = selectedVersionId;
    void Promise.resolve().then(() => loadEvaluations(versionId));
    // Re-runs on the SELECTED VERSION changing, deliberately not on
    // `loadEvaluations` changing: it is redefined every render, so depending on
    // it would refetch on each one.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedVersionId]);

  const selectedVersion = versions?.find((v) => v.id === selectedVersionId) ?? null;

  return (
    <AppShell
      title={strategyName ? `${strategyName} — Signals` : "Signals"}
      subtitle="Ask whether a validated strategy version currently says BUY, SELL or HOLD for a batch of symbols — always with a reason — then browse the recent evaluations."
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
            description="Only a validated version can be evaluated — an unvalidated one is disabled below because the backend rejects a signal check against it (409 VERSION_NOT_VALIDATED)."
          >
            <Field
              label="Strategy version"
              hint={
                selectedVersion && selectedVersion.status !== "validated"
                  ? `v${selectedVersion.version_number} is "${selectedVersion.status}" — validate it in the strategy builder before checking signals.`
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
                    {v.status !== "validated" ? " (not evaluable)" : ""}
                  </option>
                ))}
              </select>
            </Field>
          </Panel>
        )}

        {selectedVersionId && (
          <CheckSignalsForm
            strategyId={strategyId}
            versionId={selectedVersionId}
            onEvaluated={() => void loadEvaluations(selectedVersionId)}
          />
        )}

        {evaluationsError && (
          <Alert tone="error">
            {evaluationsStatus ? `HTTP ${evaluationsStatus}: ` : ""}
            {evaluationsError}
          </Alert>
        )}

        {loadingEvaluations && !evaluations && !evaluationsError && (
          <EmptyNote>Loading signal evaluations…</EmptyNote>
        )}

        {selectedVersionId && evaluations && !evaluationsError && (
          <SignalTable evaluations={evaluations} />
        )}
      </div>
    </AppShell>
  );
}
