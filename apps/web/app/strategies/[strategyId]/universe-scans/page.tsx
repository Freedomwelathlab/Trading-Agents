"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { handleExpiredSession } from "@/lib/session";
import AppShell from "@/components/shell/AppShell";
import {
  UniverseScanForm,
  formatDetail,
  type ListUniverseScansResponse,
  type UniverseScanSummary,
} from "@/components/UniverseScanForm";
import { UniverseScanList } from "@/components/UniverseScanList";
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

/**
 * Universe scans for one strategy: trigger a scan across many symbols for a
 * validated version, then browse that version's scan history (Phase 60).
 *
 * Same shape as `app/strategies/[strategyId]/backtests/page.tsx` — a
 * client component whose version selector, trigger form and scan list
 * share state that only makes sense wired together here. The version
 * selector lists every version (not just validated ones) so the reader
 * sees the whole history and why an option is unavailable; a version whose
 * `status` is not `"validated"` is disabled, because the backend 409s
 * `VERSION_NOT_VALIDATED` for anything else.
 */
export default function StrategyUniverseScansPage() {
  const params = useParams<{ strategyId: string }>();
  const strategyId = params.strategyId;

  const [strategyName, setStrategyName] = useState<string | null>(null);
  const [versions, setVersions] = useState<StrategyVersionSummary[] | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [loadingStrategy, setLoadingStrategy] = useState(false);
  const [strategyError, setStrategyError] = useState<string | null>(null);

  const [scans, setScans] = useState<UniverseScanSummary[] | null>(null);
  const [loadingScans, setLoadingScans] = useState(false);
  const [scansError, setScansError] = useState<string | null>(null);
  const [scansStatus, setScansStatus] = useState<number | null>(null);

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

  async function loadScans(versionId: string) {
    setLoadingScans(true);
    setScansError(null);
    setScansStatus(null);
    try {
      const res = await fetch(
        `/api/strategies/${strategyId}/versions/${versionId}/universe-scans?limit=50&offset=0`,
        { cache: "no-store" },
      );
      const data = (await res.json().catch(() => null)) as
        | (ListUniverseScansResponse & { detail?: unknown })
        | null;
      setScansStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setScans(null);
        setScansError(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setScans(data?.items ?? []);
    } catch {
      setScans(null);
      setScansError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoadingScans(false);
    }
  }

  useEffect(() => {
    if (selectedVersionId) void loadScans(selectedVersionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedVersionId]);

  const selectedVersion = versions?.find((v) => v.id === selectedVersionId) ?? null;

  return (
    <AppShell
      title={strategyName ? `${strategyName} — Universe scans` : "Universe scans"}
      subtitle="Run one validated strategy version across many symbols at once, then browse the ranked results."
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
            description="Only a validated version can be scanned — an unvalidated one is disabled below because the backend rejects a scan against it (409 VERSION_NOT_VALIDATED)."
          >
            <Field
              label="Strategy version"
              hint={
                selectedVersion && selectedVersion.status !== "validated"
                  ? `v${selectedVersion.version_number} is "${selectedVersion.status}" — validate it in the strategy builder before running a scan.`
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
                    {v.status !== "validated" ? " (not scannable)" : ""}
                  </option>
                ))}
              </select>
            </Field>
          </Panel>
        )}

        {selectedVersionId && (
          <UniverseScanForm
            strategyId={strategyId}
            versionId={selectedVersionId}
            onScanComplete={() => void loadScans(selectedVersionId)}
          />
        )}

        {scansError && (
          <Alert tone="error">
            {scansStatus ? `HTTP ${scansStatus}: ` : ""}
            {scansError}
          </Alert>
        )}

        {loadingScans && !scans && !scansError && (
          <EmptyNote>Loading universe scans…</EmptyNote>
        )}

        {selectedVersionId && scans && !scansError && (
          <UniverseScanList
            strategyId={strategyId}
            versionId={selectedVersionId}
            scans={scans}
          />
        )}
      </div>
    </AppShell>
  );
}
