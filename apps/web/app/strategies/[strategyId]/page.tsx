"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { handleExpiredSession } from "@/lib/session";
import AppShell from "@/components/shell/AppShell";
import StrategyBuilderForm, {
  type StrategyVersionResponse,
} from "@/components/StrategyBuilderForm";
import {
  Alert,
  EmptyNote,
  Field,
  Panel,
  Pill,
  SectionHeading,
  TableScroll,
  btnGhost,
  btnPrimary,
  btnSecondary,
  inputClass,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
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
  owner_user_id: string;
  name: string;
  description: string | null;
  status: "active" | "archived";
  created_at: string;
  updated_at: string;
  latest_version: StrategyVersionResponse;
  versions: StrategyVersionSummary[];
};

/**
 * FastAPI returns a plain string `detail` for hand-raised `HTTPException`s
 * but a list of objects for a 422 request-shape validation error. Mirrors
 * `BacktestPanel.formatDetail` / `StrategyList.formatDetail`.
 */
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

function statusTone(status: string): "pos" | "neg" | "warn" | "neutral" {
  if (status === "active" || status === "validated") return "pos";
  if (status === "archived") return "neutral";
  if (status === "draft") return "warn";
  return "neutral";
}

/**
 * Strategy detail: header (with inline edit), version history and the
 * form-based builder for the selected version (Phase 54).
 *
 * This page is a Client Component in its entirety — unlike
 * `app/dashboard/page.tsx`'s server-component-composes-client-panels shape
 * — because every piece of it (the fetch, the version selector, the fork
 * action, the inline edit form) shares state that only makes sense wired
 * together in one place, and no separate "detail" component is in this
 * phase's scope. `AppShell` and `StrategyBuilderForm` are still plain
 * components rendered from here exactly as they would be from a server
 * component.
 */
export default function StrategyDetailPage() {
  const params = useParams<{ strategyId: string }>();
  const strategyId = params.strategyId;

  const [detail, setDetail] = useState<StrategyDetailResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  const [selectedVersion, setSelectedVersion] = useState<StrategyVersionResponse | null>(null);
  const [versionLoading, setVersionLoading] = useState(false);
  const [versionError, setVersionError] = useState<string | null>(null);

  const [forking, setForking] = useState(false);
  const [forkError, setForkError] = useState<string | null>(null);

  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [editStatus, setEditStatus] = useState<"active" | "archived">("active");
  const [patchSaving, setPatchSaving] = useState(false);
  const [patchError, setPatchError] = useState<string | null>(null);

  async function loadDetail(): Promise<StrategyDetailResponse | null> {
    setLoading(true);
    setErrorDetail(null);
    setStatus(null);
    try {
      const res = await fetch(`/api/strategies/${strategyId}`, { cache: "no-store" });
      const data = (await res.json().catch(() => null)) as
        | (StrategyDetailResponse & { detail?: unknown })
        | null;
      setStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return null;
        setDetail(null);
        setErrorDetail(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
        return null;
      }
      setDetail(data);
      return data;
    } catch {
      setDetail(null);
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
      return null;
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void (async () => {
      const data = await loadDetail();
      if (data) setSelectedVersion(data.latest_version);
    })();
    // Load once per strategyId; navigating between strategies remounts via
    // the URL change, but the effect still guards on the id explicitly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyId]);

  async function selectVersion(summary: StrategyVersionSummary) {
    setVersionError(null);
    if (detail && summary.id === detail.latest_version.id) {
      setSelectedVersion(detail.latest_version);
      return;
    }
    setVersionLoading(true);
    try {
      const res = await fetch(`/api/strategies/${strategyId}/versions/${summary.id}`, {
        cache: "no-store",
      });
      const data = (await res.json().catch(() => null)) as
        | (StrategyVersionResponse & { detail?: unknown })
        | null;
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setVersionError(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setSelectedVersion(data);
    } catch {
      setVersionError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setVersionLoading(false);
    }
  }

  async function handleFork() {
    setForking(true);
    setForkError(null);
    try {
      const res = await fetch(`/api/strategies/${strategyId}/versions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = (await res.json().catch(() => null)) as
        | (StrategyVersionResponse & { detail?: unknown })
        | null;
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setForkError(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      await loadDetail();
      setSelectedVersion(data);
    } catch {
      setForkError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setForking(false);
    }
  }

  function startEdit() {
    if (!detail) return;
    setEditName(detail.name);
    setEditDescription(detail.description ?? "");
    setEditStatus(detail.status);
    setPatchError(null);
    setEditing(true);
  }

  async function handlePatch(e: React.FormEvent) {
    e.preventDefault();
    if (!detail) return;
    setPatchSaving(true);
    setPatchError(null);
    try {
      // Only the keys the user actually changed — the backend applies only
      // present keys.
      const body: Record<string, unknown> = {};
      if (editName !== detail.name) body.name = editName;
      const normalizedDescription = editDescription.trim() === "" ? null : editDescription;
      if (normalizedDescription !== (detail.description ?? null)) {
        body.description = normalizedDescription;
      }
      if (editStatus !== detail.status) body.status = editStatus;

      if (Object.keys(body).length === 0) {
        setEditing(false);
        return;
      }

      const res = await fetch(`/api/strategies/${strategyId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await res.json().catch(() => null)) as { detail?: unknown } | null;
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setPatchError(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setEditing(false);
      await loadDetail();
    } catch {
      setPatchError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setPatchSaving(false);
    }
  }

  return (
    <AppShell
      title={detail ? detail.name : "Strategy"}
      subtitle="Version history and the form-based rule builder for this strategy's selected version."
    >
      <div className="flex flex-col gap-5">
        {errorDetail && (
          <Alert tone="error">
            {status ? `HTTP ${status}: ` : ""}
            {errorDetail}
          </Alert>
        )}

        {loading && !detail && !errorDetail && <EmptyNote>Loading strategy…</EmptyNote>}

        {detail && (
          <>
            <Panel
              title="Strategy"
              actions={
                !editing ? (
                  <button type="button" className={btnGhost} onClick={startEdit}>
                    Edit
                  </button>
                ) : undefined
              }
            >
              {!editing ? (
                <div className="flex flex-col gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-lg font-semibold text-ink">{detail.name}</h2>
                    <Pill tone={statusTone(detail.status)}>{detail.status}</Pill>
                  </div>
                  <p className="text-sm text-ink-muted">
                    {detail.description || "No description."}
                  </p>
                </div>
              ) : (
                <form
                  onSubmit={(e) => void handlePatch(e)}
                  className="grid gap-3 sm:grid-cols-3 sm:items-end"
                >
                  <Field label="Name">
                    <input
                      className={inputClass}
                      value={editName}
                      onChange={(e) => setEditName(e.target.value)}
                      required
                    />
                  </Field>
                  <Field label="Description">
                    <input
                      className={inputClass}
                      value={editDescription}
                      onChange={(e) => setEditDescription(e.target.value)}
                    />
                  </Field>
                  <Field label="Status">
                    <select
                      className={inputClass}
                      value={editStatus}
                      onChange={(e) => setEditStatus(e.target.value as "active" | "archived")}
                    >
                      <option value="active">active</option>
                      <option value="archived">archived</option>
                    </select>
                  </Field>
                  <div className="flex gap-2 sm:col-span-3">
                    <button type="submit" disabled={patchSaving} className={btnPrimary}>
                      {patchSaving ? "Saving…" : "Save"}
                    </button>
                    <button
                      type="button"
                      className={btnGhost}
                      disabled={patchSaving}
                      onClick={() => setEditing(false)}
                    >
                      Cancel
                    </button>
                  </div>
                </form>
              )}
              {patchError && <Alert tone="error">{patchError}</Alert>}
            </Panel>

            <SectionHeading note="Newest version_number first">Versions</SectionHeading>
            <Panel
              title="Version history"
              actions={
                <button
                  type="button"
                  className={btnSecondary}
                  disabled={forking}
                  onClick={() => void handleFork()}
                >
                  {forking ? "Forking…" : "Fork new draft"}
                </button>
              }
            >
              {forkError && <Alert tone="error">{forkError}</Alert>}
              {versionError && <Alert tone="error">{versionError}</Alert>}
              <TableScroll>
                <table className={tableClass}>
                  <caption className="sr-only">
                    Every version of this strategy, exactly as returned by the API
                  </caption>
                  <thead>
                    <tr className={theadRowClass}>
                      <th className={thClass}>Version</th>
                      <th className={thClass}>Status</th>
                      <th className={thClass}>Created</th>
                      <th className={thClass}>Validated</th>
                      <th className={thClass}>Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.versions.map((v) => (
                      <tr key={v.id} className={tbodyRowClass} data-testid="version-row">
                        <td className={tdClass}>v{v.version_number}</td>
                        <td className={tdClass}>
                          <Pill tone={statusTone(v.status)}>{v.status}</Pill>
                        </td>
                        <td className={`${tdClass} text-ink-muted`}>{v.created_at}</td>
                        <td className={`${tdClass} text-ink-muted`}>
                          {v.validated_at ?? "—"}
                        </td>
                        <td className={tdClass}>
                          {selectedVersion?.id === v.id ? (
                            <Pill tone="neutral">Selected</Pill>
                          ) : (
                            <button
                              type="button"
                              className={btnGhost}
                              onClick={() => void selectVersion(v)}
                            >
                              Select v{v.version_number}
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableScroll>
            </Panel>

            {versionLoading && <EmptyNote>Loading version…</EmptyNote>}
            {selectedVersion && !versionLoading && (
              <StrategyBuilderForm
                key={selectedVersion.id}
                strategyId={strategyId}
                version={selectedVersion}
                onVersionUpdate={(updated) => {
                  setSelectedVersion(updated);
                  void loadDetail();
                }}
              />
            )}
          </>
        )}
      </div>
    </AppShell>
  );
}
