"use client";

import { useCallback, useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import { selectBroker } from "@/lib/brokerSelection";

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
    <section className="rounded border border-neutral-300 p-4 dark:border-neutral-700">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        My brokers
      </h2>
      <p className="mb-3 text-xs text-neutral-500">
        Brokers you hold an access grant for. “Use” fills this broker&rsquo;s id into the trade,
        agent-trade and portfolio forms below — the backend still re-checks your grant on every
        request.
      </p>

      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Limit (1–500)
          <input
            type="number"
            min={1}
            max={500}
            className="w-28 rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Offset
          <input
            type="number"
            min={0}
            className="w-28 rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
            value={offset}
            onChange={(e) => setOffset(e.target.value)}
          />
        </label>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          className="rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
        >
          {loading ? "Loading…" : "Refresh brokers"}
        </button>
      </div>

      {errorDetail && (
        <p
          role="alert"
          className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300"
        >
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </p>
      )}

      {rows && rows.length === 0 && (
        <p className="mt-3 text-sm text-neutral-500">
          No brokers on this page. If you expect access to one, ask an admin for a broker grant.
        </p>
      )}

      {rows && rows.length > 0 && (
        <table className="mt-3 w-full text-left text-xs">
          <thead>
            <tr className="uppercase tracking-wide text-neutral-500">
              <th className="pr-3">id</th>
              <th className="pr-3">name</th>
              <th className="pr-3">kind</th>
              <th className="pr-3">provider</th>
              <th className="pr-3">is_active</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((b) => (
              <tr key={b.id}>
                <td className="pr-3 font-mono">{b.id}</td>
                <td className="pr-3">{b.name}</td>
                <td className="pr-3">{b.kind}</td>
                <td className="pr-3">{b.provider}</td>
                <td className="pr-3">{String(b.is_active)}</td>
                <td className="pr-3">
                  <button
                    type="button"
                    onClick={() => use(b.id)}
                    className="rounded border border-neutral-300 px-2 py-1 dark:border-neutral-700"
                  >
                    {selectedId === b.id ? "In use" : "Use"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
