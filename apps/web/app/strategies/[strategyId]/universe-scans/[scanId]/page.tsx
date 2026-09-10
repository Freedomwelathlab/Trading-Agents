"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { handleExpiredSession } from "@/lib/session";
import AppShell from "@/components/shell/AppShell";
import {
  formatDetail,
  type UniverseScanDetailResponse,
} from "@/components/UniverseScanForm";
import { UniverseScanResults } from "@/components/UniverseScanResults";
import { Alert, EmptyNote } from "@/components/ui/primitives";

/**
 * One universe scan's ranked results (Phase 60) — `GET /universe-scans/{scanId}`.
 *
 * Entirely a client component, `useParams`-driven, matching
 * `app/strategies/[strategyId]/backtests/[runId]/page.tsx`'s shape: the
 * fetch, loading and error state here have nothing else in this phase's
 * scope to compose with. `strategyId` comes from the route so each result
 * row can link to that symbol's Phase 56 backtest-run detail page.
 */
export default function UniverseScanDetailPage() {
  const params = useParams<{ strategyId: string; scanId: string }>();
  const { strategyId, scanId } = params;

  const [scan, setScan] = useState<UniverseScanDetailResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  useEffect(() => {
    void (async () => {
      setLoading(true);
      setErrorDetail(null);
      setStatus(null);
      try {
        const res = await fetch(`/api/universe-scans/${encodeURIComponent(scanId)}`, {
          cache: "no-store",
        });
        const data = (await res.json().catch(() => null)) as
          | (UniverseScanDetailResponse & { detail?: unknown })
          | null;
        setStatus(res.status);
        if (!res.ok) {
          if (handleExpiredSession(res.status)) return;
          setScan(null);
          setErrorDetail(
            formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`,
          );
          return;
        }
        setScan(data);
      } catch {
        setScan(null);
        setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
      } finally {
        setLoading(false);
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    })();
  }, [scanId]);

  return (
    <AppShell
      title="Universe scan"
      subtitle="Ranked per-symbol results for one universe scan: return, drawdown, win rate and trade count, best-first."
    >
      <div className="flex flex-col gap-5">
        {errorDetail && (
          <Alert tone="error">
            {status ? `HTTP ${status}: ` : ""}
            {errorDetail}
          </Alert>
        )}

        {loading && !scan && !errorDetail && (
          <EmptyNote>Loading universe scan…</EmptyNote>
        )}

        {scan && !errorDetail && (
          <UniverseScanResults scan={scan} strategyId={strategyId} />
        )}
      </div>
    </AppShell>
  );
}
