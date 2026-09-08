"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  EmptyNote,
  Field,
  Panel,
  Pill,
  TableScroll,
  btnPrimary,
  inputClass,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";

export type StrategySummary = {
  id: string;
  name: string;
  description: string | null;
  status: "active" | "archived";
  created_at: string;
  updated_at: string;
  latest_version_number: number;
  latest_version_status: "draft" | "validated" | "archived";
};

export type ListStrategiesResponse = {
  items?: StrategySummary[];
  limit?: number;
  offset?: number;
  detail?: unknown;
};

/**
 * FastAPI returns a plain string `detail` for hand-raised `HTTPException`s
 * but a list of objects for a 422 request-shape validation error (e.g. an
 * empty `name` on create). Both are rendered as what they actually are —
 * mirrors `BacktestPanel.formatDetail`.
 */
export function formatDetail(detail: unknown): string | null {
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
 * Strategy Lab list + create (Phase 54).
 *
 * Fetches `GET /api/strategies` on mount and after every create — a plain
 * refetch rather than a locally-spliced array, matching this codebase's
 * existing "plain fetch, no cache" convention (see `Watchlist.tsx`). Every
 * error renders the backend's real `detail` text verbatim.
 */
export default function StrategyList() {
  const [items, setItems] = useState<StrategySummary[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);

  async function load() {
    setLoading(true);
    setErrorDetail(null);
    setStatus(null);
    try {
      const res = await fetch("/api/strategies?limit=50&offset=0", { cache: "no-store" });
      const data = (await res.json().catch(() => null)) as ListStrategiesResponse | null;
      setStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setItems(null);
        setErrorDetail(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setItems(data?.items ?? []);
    } catch {
      setItems(null);
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // Load once on mount only, matching the other list panels in this app
    // (e.g. `AdminListings.tsx`).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setCreating(true);
    setErrorDetail(null);
    setStatus(null);
    try {
      const body: Record<string, unknown> = { name };
      if (description.trim() !== "") body.description = description;

      const res = await fetch("/api/strategies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await res.json().catch(() => null)) as { detail?: unknown } | null;
      setStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setErrorDetail(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setName("");
      setDescription("");
      await load();
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setCreating(false);
    }
  }

  return (
    <Panel
      title="Strategies"
      description="Every strategy you own, with its latest version's status. Nothing here is a template or a preview — each row is a real, saved strategy."
    >
      <form
        onSubmit={handleCreate}
        className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end"
      >
        <Field label="Name">
          <input
            className={inputClass}
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </Field>
        <Field label="Description (optional)">
          <input
            className={inputClass}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
        <button type="submit" disabled={creating} className={btnPrimary}>
          {creating ? "Creating…" : "Create strategy"}
        </button>
      </form>

      {errorDetail && (
        <Alert tone="error">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </Alert>
      )}

      {loading && items === null && !errorDetail && (
        <EmptyNote>Loading strategies…</EmptyNote>
      )}

      {items !== null && !errorDetail && items.length === 0 && (
        <EmptyNote>
          You have no strategies yet — create one above to open its builder.
        </EmptyNote>
      )}

      {items !== null && !errorDetail && items.length > 0 && (
        <TableScroll>
          <table className={tableClass}>
            <caption className="sr-only">
              Your strategies, exactly as returned by the API
            </caption>
            <thead>
              <tr className={theadRowClass}>
                <th className={thClass}>Name</th>
                <th className={thClass}>Status</th>
                <th className={thClass}>Latest version</th>
                <th className={thClass}>Updated</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id} className={tbodyRowClass} data-testid="strategy-row">
                  <td className={tdClass}>
                    <Link
                      href={`/strategies/${s.id}`}
                      className="font-semibold text-ink underline-offset-2 hover:text-accent hover:underline"
                    >
                      {s.name}
                    </Link>
                  </td>
                  <td className={tdClass}>
                    <Pill tone={statusTone(s.status)}>{s.status}</Pill>
                  </td>
                  <td className={tdClass}>
                    v{s.latest_version_number}{" "}
                    <Pill tone={statusTone(s.latest_version_status)}>
                      {s.latest_version_status}
                    </Pill>
                  </td>
                  <td className={`${tdClass} text-ink-muted`}>{s.updated_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}
    </Panel>
  );
}
