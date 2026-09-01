"use client";

import { useCallback, useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import { selectBroker } from "@/lib/brokerSelection";
import {
  Alert,
  EmptyNote,
  Field,
  Panel,
  Pill,
  TableScroll,
  btnGhost,
  btnPrimary,
  inputClass,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";

/**
 * Lists the brokers this user actually has access to (D034's
 * `GET /brokers`, proxied by `app/api/brokers/route.ts`), and lets them
 * push a row's id into every broker-scoped form on the dashboard instead
 * of pasting a UUID by hand.
 *
 * Every row is a row the backend returned for this caller. A user with no
 * grants sees an explicit empty state, a 401 redirects to login (D032),
 * and an unreachable API renders the real DATA_UNAVAILABLE sentinel —
 * nothing here is ever filled in with a sample or placeholder broker.
 *
 * Phase 45 / D060 restyle: this panel keeps its position ahead of every
 * broker-scoped panel on the page, because it is where the `broker_id`
 * those panels need comes from. The fetch, the states and the "Use"
 * behaviour are unchanged.
 */

type BrokerRow = {
  id: string;
  name: string;
  kind: string;
  provider: string;
  is_active: boolean;
};

export default function BrokerDiscovery() {
  const [limit, setLimit] = useState("50");
  const [offset, setOffset] = useState("0");
  const [loading, setLoading] = useState(false);
  const [rows, setRows] = useState<BrokerRow[] | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setRows(null);
    setErrorDetail(null);
    setStatus(null);
    const query = new URLSearchParams({ limit, offset });
    try {
      const res = await fetch(`/api/brokers?${query.toString()}`);
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      setStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setErrorDetail((data?.detail as string | undefined) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setRows((data?.brokers as BrokerRow[] | undefined) ?? []);
    } catch {
      setStatus(null);
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }, [limit, offset]);

  useEffect(() => {
    void load();
    // Load once on mount; later loads are the explicit Refresh button, so
    // editing limit/offset never fires a request the user didn't ask for.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function use(brokerId: string) {
    selectBroker(brokerId);
    setSelectedId(brokerId);
  }

  return (
    <Panel
      title="My brokers"
      description={
        <>
          Brokers you hold an access grant for. “Use” fills this broker&rsquo;s id
          into the trade, agent-trade and portfolio panels below — the backend
          still re-checks your grant on every request.
        </>
      }
      actions={
        selectedId ? (
          <Pill tone="pos" testId="selected-broker-pill">
            <span className="text-ink-faint">in use</span>
            <span className="max-w-[18ch] truncate">{selectedId}</span>
          </Pill>
        ) : null
      }
    >
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Limit (1–500)" className="w-32">
          <input
            type="number"
            min={1}
            max={500}
            className={inputClass}
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
          />
        </Field>
        <Field label="Offset" className="w-32">
          <input
            type="number"
            min={0}
            className={inputClass}
            value={offset}
            onChange={(e) => setOffset(e.target.value)}
          />
        </Field>
        <button type="button" onClick={load} disabled={loading} className={btnPrimary}>
          {loading ? "Loading…" : "Refresh brokers"}
        </button>
      </div>

      {errorDetail && (
        <Alert tone="error">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </Alert>
      )}

      {rows && rows.length === 0 && (
        <EmptyNote>
          No brokers on this page. If you expect access to one, ask an admin for a
          broker grant.
        </EmptyNote>
      )}

      {rows && rows.length > 0 && (
        <TableScroll>
          <table className={tableClass}>
            <thead>
              <tr className={theadRowClass}>
                <th className={thClass}>id</th>
                <th className={thClass}>name</th>
                <th className={thClass}>kind</th>
                <th className={thClass}>provider</th>
                <th className={thClass}>is_active</th>
                <th className={thClass} />
              </tr>
            </thead>
            <tbody>
              {rows.map((b) => {
                const active = selectedId === b.id;
                return (
                  <tr
                    key={b.id}
                    className={`${tbodyRowClass} ${active ? "bg-accent/8" : ""}`}
                  >
                    <td className={`${tdClass} text-ink-muted`}>{b.id}</td>
                    <td className={`${tdClass} font-sans font-medium`}>{b.name}</td>
                    <td className={tdClass}>{b.kind}</td>
                    <td className={tdClass}>{b.provider}</td>
                    <td className={tdClass}>
                      <Pill tone={b.is_active ? "pos" : "neutral"}>
                        {String(b.is_active)}
                      </Pill>
                    </td>
                    <td className={`${tdClass} text-right`}>
                      <button
                        type="button"
                        onClick={() => use(b.id)}
                        className={
                          active
                            ? `${btnGhost} border-accent text-accent`
                            : btnGhost
                        }
                      >
                        {active ? "In use" : "Use"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </TableScroll>
      )}
    </Panel>
  );
}
