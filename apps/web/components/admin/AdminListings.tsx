"use client";

import { useCallback, useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";

/**
 * Read-only listing views for the D030 admin listing endpoints.
 *
 * These close the gap every earlier admin form had: `PATCH
 * /admin/users/{id}` and `DELETE /admin/broker-grants/{id}` both need an
 * id the operator previously had no way to discover through the app.
 *
 * Every row shown here is a row the backend returned. A caller without
 * `admin:manage` sees the backend's real 403 rendered as text — the
 * listing is never replaced by sample rows, and an empty page is shown
 * as an empty page.
 */

type ListState<T> = {
  loading: boolean;
  rows: T[] | null;
  errorDetail: string | null;
  status: number | null;
};

const INITIAL: ListState<never> = {
  loading: false,
  rows: null,
  errorDetail: null,
  status: null,
};

function useAdminList<T>(path: string, key: string) {
  const [limit, setLimit] = useState("50");
  const [offset, setOffset] = useState("0");
  const [state, setState] = useState<ListState<T>>(INITIAL);

  const load = useCallback(async () => {
    setState({ loading: true, rows: null, errorDetail: null, status: null });
    const query = new URLSearchParams({ limit, offset });
    try {
      const res = await fetch(`${path}?${query.toString()}`);
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      if (!res.ok) {
        if (handleExpiredSession(res.status)) {
          setState({ loading: false, rows: null, errorDetail: null, status: res.status });
          return;
        }
        setState({
          loading: false,
          rows: null,
          status: res.status,
          errorDetail:
            (data?.detail as string | undefined) ?? `Request failed (HTTP ${res.status})`,
        });
        return;
      }
      setState({
        loading: false,
        rows: (data?.[key] as T[] | undefined) ?? [],
        errorDetail: null,
        status: res.status,
      });
    } catch {
      setState({
        loading: false,
        rows: null,
        status: null,
        errorDetail: "DATA_UNAVAILABLE: could not reach the trading API",
      });
    }
  }, [path, key, limit, offset]);

  return { limit, setLimit, offset, setOffset, state, load };
}

function Pager({
  limit,
  setLimit,
  offset,
  setOffset,
  loading,
  onLoad,
  label,
}: {
  limit: string;
  setLimit: (v: string) => void;
  offset: string;
  setOffset: (v: string) => void;
  loading: boolean;
  onLoad: () => void;
  label: string;
}) {
  return (
    <div className="flex flex-wrap items-end gap-3">
      <label className="flex flex-col gap-1 text-sm">
        {label} limit (1–500)
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
        {label} offset
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
        onClick={onLoad}
        disabled={loading}
        className="rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
      >
        {loading ? "Loading…" : `Refresh ${label.toLowerCase()}`}
      </button>
    </div>
  );
}

function ListError({ status, detail }: { status: number | null; detail: string }) {
  return (
    <p
      role="alert"
      className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300"
    >
      {status ? `HTTP ${status}: ` : ""}
      {detail}
    </p>
  );
}

type UserRow = { id: string; email: string; is_active: boolean; role_id: string | null };
type RoleRow = {
  id: string;
  name: string;
  description: string | null;
  permissions: string[];
};
type GrantRow = { id: string; user_id: string; broker_id: string };

export function UsersList() {
  const { limit, setLimit, offset, setOffset, state, load } = useAdminList<UserRow>(
    "/api/admin/users",
    "users",
  );
  useEffect(() => {
    void load();
    // Load once on mount only; subsequent loads are the explicit Refresh
    // button, so changing limit/offset never fires a request the operator
    // didn't ask for.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <section className="rounded border border-neutral-300 p-4 dark:border-neutral-700">
      <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        Users
      </h3>
      <Pager
        limit={limit}
        setLimit={setLimit}
        offset={offset}
        setOffset={setOffset}
        loading={state.loading}
        onLoad={load}
        label="Users"
      />
      {state.errorDetail && <ListError status={state.status} detail={state.errorDetail} />}
      {state.rows && state.rows.length === 0 && (
        <p className="mt-3 text-sm text-neutral-500">No users on this page.</p>
      )}
      {state.rows && state.rows.length > 0 && (
        <table className="mt-3 w-full text-left text-xs">
          <thead>
            <tr className="uppercase tracking-wide text-neutral-500">
              <th className="pr-3">id</th>
              <th className="pr-3">email</th>
              <th className="pr-3">is_active</th>
              <th className="pr-3">role_id</th>
            </tr>
          </thead>
          <tbody>
            {state.rows.map((u) => (
              <tr key={u.id}>
                <td className="pr-3 font-mono">{u.id}</td>
                <td className="pr-3">{u.email}</td>
                <td className="pr-3">{String(u.is_active)}</td>
                <td className="pr-3 font-mono">{u.role_id ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function RolesList() {
  const { limit, setLimit, offset, setOffset, state, load } = useAdminList<RoleRow>(
    "/api/admin/roles",
    "roles",
  );
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <section className="rounded border border-neutral-300 p-4 dark:border-neutral-700">
      <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        Roles
      </h3>
      <Pager
        limit={limit}
        setLimit={setLimit}
        offset={offset}
        setOffset={setOffset}
        loading={state.loading}
        onLoad={load}
        label="Roles"
      />
      {state.errorDetail && <ListError status={state.status} detail={state.errorDetail} />}
      {state.rows && state.rows.length === 0 && (
        <p className="mt-3 text-sm text-neutral-500">No roles on this page.</p>
      )}
      {state.rows && state.rows.length > 0 && (
        <table className="mt-3 w-full text-left text-xs">
          <thead>
            <tr className="uppercase tracking-wide text-neutral-500">
              <th className="pr-3">id</th>
              <th className="pr-3">name</th>
              <th className="pr-3">description</th>
              <th className="pr-3">permissions</th>
            </tr>
          </thead>
          <tbody>
            {state.rows.map((r) => (
              <tr key={r.id}>
                <td className="pr-3 font-mono">{r.id}</td>
                <td className="pr-3">{r.name}</td>
                <td className="pr-3">{r.description ?? "—"}</td>
                <td className="pr-3 font-mono">
                  {r.permissions.length > 0 ? r.permissions.join(", ") : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function BrokerGrantsList() {
  const { limit, setLimit, offset, setOffset, state, load } = useAdminList<GrantRow>(
    "/api/admin/broker-grants",
    "grants",
  );
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <section className="rounded border border-neutral-300 p-4 dark:border-neutral-700">
      <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        Broker grants
      </h3>
      <p className="mb-2 text-xs text-neutral-500">
        The <code>id</code> column is the grant handle the revoke form needs.
      </p>
      <Pager
        limit={limit}
        setLimit={setLimit}
        offset={offset}
        setOffset={setOffset}
        loading={state.loading}
        onLoad={load}
        label="Grants"
      />
      {state.errorDetail && <ListError status={state.status} detail={state.errorDetail} />}
      {state.rows && state.rows.length === 0 && (
        <p className="mt-3 text-sm text-neutral-500">No broker grants on this page.</p>
      )}
      {state.rows && state.rows.length > 0 && (
        <table className="mt-3 w-full text-left text-xs">
          <thead>
            <tr className="uppercase tracking-wide text-neutral-500">
              <th className="pr-3">id</th>
              <th className="pr-3">user_id</th>
              <th className="pr-3">broker_id</th>
            </tr>
          </thead>
          <tbody>
            {state.rows.map((g) => (
              <tr key={g.id}>
                <td className="pr-3 font-mono">{g.id}</td>
                <td className="pr-3 font-mono">{g.user_id}</td>
                <td className="pr-3 font-mono">{g.broker_id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
