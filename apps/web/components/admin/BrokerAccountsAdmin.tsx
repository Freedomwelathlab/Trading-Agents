"use client";

import { useCallback, useEffect, useState } from "react";
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

/**
 * Broker housekeeping (Phase 97, D116): every broker row, with a tick box
 * each, and three actions on the ticked ones.
 *
 * - **Approve for desk / Hide from desk** set `is_active`. The trading
 *   desk lists only approved brokers; hiding changes nothing else.
 * - **Delete** is answered by the backend PER BROKER: a broker with no
 *   orders and nothing running is deleted; one with order history is
 *   archived (hidden, history kept) because orders and fills are the audit
 *   trail; one with a bot or deployment still running is refused. Every
 *   outcome is shown as the backend worded it - this component never
 *   decides which of the three a broker gets.
 */

type AdminBroker = {
  id: string;
  name: string;
  kind: string;
  provider: string;
  approved: boolean;
  adapter_status: string;
  created_at: string;
  order_count: number;
  running_bots: number;
  running_deployments: number;
};

type DeleteOutcome = {
  broker_id: string;
  name: string | null;
  outcome: "deleted" | "archived" | "refused" | "not_found";
  detail: string;
};

const OUTCOME_TONE = {
  deleted: "pos",
  archived: "warn",
  refused: "neg",
  not_found: "neutral",
} as const;

export default function BrokerAccountsAdmin() {
  const [rows, setRows] = useState<AdminBroker[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [outcomes, setOutcomes] = useState<DeleteOutcome[] | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await fetch("/api/admin/brokers");
      const data = (await res.json().catch(() => null)) as
        | { brokers?: AdminBroker[]; detail?: string }
        | null;
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setError(data?.detail ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setRows(data?.brokers ?? []);
      // Drop ticks for brokers that no longer exist.
      setSelected((prev) => {
        const ids = new Set((data?.brokers ?? []).map((b) => b.id));
        return new Set([...prev].filter((id) => ids.has(id)));
      });
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    }
  }, []);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    if (!rows) return;
    setSelected((prev) =>
      prev.size === rows.length ? new Set() : new Set(rows.map((r) => r.id)),
    );
  }

  async function post(path: string, body: unknown): Promise<Record<string, unknown> | null> {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
    if (!res.ok) {
      if (handleExpiredSession(res.status)) return null;
      setError((data?.detail as string | undefined) ?? `Request failed (HTTP ${res.status})`);
      return null;
    }
    return data;
  }

  async function setApproval(approved: boolean) {
    if (selected.size === 0) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    setOutcomes(null);
    try {
      const data = await post("/api/admin/brokers/approval", {
        broker_ids: [...selected],
        approved,
      });
      if (data) {
        setNotice(
          `${String(data.updated)} broker(s) ${approved ? "approved for" : "hidden from"} the trading desk.`,
        );
        await load();
      }
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setBusy(false);
    }
  }

  async function deleteSelected() {
    if (selected.size === 0) return;
    const ok = window.confirm(
      `Delete ${selected.size} broker(s)? Brokers with order history are archived instead ` +
        "(hidden, history kept); brokers with a running bot or deployment are refused.",
    );
    if (!ok) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    setOutcomes(null);
    try {
      const data = await post("/api/admin/brokers/delete", { broker_ids: [...selected] });
      if (data) {
        setOutcomes((data.results as DeleteOutcome[] | undefined) ?? []);
        await load();
      }
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setBusy(false);
    }
  }

  const allTicked = rows !== null && rows.length > 0 && selected.size === rows.length;

  return (
    <Panel
      title="Broker accounts"
      description={
        <>
          Every broker on this deployment. The trading desk lists only{" "}
          <strong>approved</strong> brokers. Tick brokers to approve, hide or delete them.
          Deleting never removes order history: a broker that has traded is archived instead.
        </>
      }
      actions={
        <button type="button" className={btnGhost} onClick={() => void load()} disabled={busy}>
          Refresh
        </button>
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className={btnSecondary}
          disabled={busy || selected.size === 0}
          onClick={() => void setApproval(true)}
        >
          Approve for desk
        </button>
        <button
          type="button"
          className={btnSecondary}
          disabled={busy || selected.size === 0}
          onClick={() => void setApproval(false)}
        >
          Hide from desk
        </button>
        <button
          type="button"
          className={`${btnSecondary} hover:border-red-500 hover:text-neg`}
          disabled={busy || selected.size === 0}
          onClick={() => void deleteSelected()}
        >
          Delete selected
        </button>
        <span className="text-xs text-ink-faint">{selected.size} selected</span>
      </div>

      {error ? <Alert tone="error">{error}</Alert> : null}
      {notice ? (
        <Alert tone="warn" role="status">
          {notice}
        </Alert>
      ) : null}

      {outcomes && outcomes.length > 0 ? (
        <ul className="flex flex-col gap-1 text-xs" data-testid="delete-outcomes">
          {outcomes.map((o) => (
            <li key={o.broker_id} className="flex flex-wrap items-center gap-2">
              <Pill tone={OUTCOME_TONE[o.outcome]}>{o.outcome}</Pill>
              <span className="font-medium">{o.name ?? o.broker_id}</span>
              <span className="text-ink-faint">{o.detail}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {rows && rows.length === 0 ? <EmptyNote>No brokers exist yet.</EmptyNote> : null}

      {rows && rows.length > 0 ? (
        <TableScroll>
          <table className={tableClass}>
            <thead>
              <tr className={theadRowClass}>
                <th className={thClass}>
                  <input
                    type="checkbox"
                    aria-label="Select all brokers"
                    checked={allTicked}
                    onChange={toggleAll}
                  />
                </th>
                <th className={thClass}>name</th>
                <th className={thClass}>provider</th>
                <th className={thClass}>kind</th>
                <th className={thClass}>adapter</th>
                <th className={thClass}>desk</th>
                <th className={thClass}>orders</th>
                <th className={thClass}>running</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((b) => (
                <tr key={b.id} className={tbodyRowClass}>
                  <td className={tdClass}>
                    <input
                      type="checkbox"
                      aria-label={`Select ${b.name}`}
                      checked={selected.has(b.id)}
                      onChange={() => toggle(b.id)}
                    />
                  </td>
                  <td className={`${tdClass} font-sans font-medium`}>
                    {b.name}
                    <div className="text-[10px] text-ink-faint">{b.id}</div>
                  </td>
                  <td className={tdClass}>{b.provider}</td>
                  <td className={tdClass}>{b.kind}</td>
                  <td className={tdClass}>
                    <Pill
                      tone={
                        b.adapter_status === "implemented" || b.adapter_status === "built_in"
                          ? "pos"
                          : "neutral"
                      }
                    >
                      {b.adapter_status}
                    </Pill>
                  </td>
                  <td className={tdClass}>
                    <Pill tone={b.approved ? "pos" : "neutral"}>
                      {b.approved ? "approved" : "hidden"}
                    </Pill>
                  </td>
                  <td className={tdClass}>{b.order_count}</td>
                  <td className={tdClass}>
                    {b.running_bots + b.running_deployments > 0
                      ? `${b.running_bots} bot · ${b.running_deployments} deploy`
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      ) : null}
    </Panel>
  );
}
