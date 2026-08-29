"use client";

import { useState } from "react";
import { handleExpiredSession } from "@/lib/session";

type UserResponse = {
  id?: string;
  email?: string;
  is_active?: boolean;
  role_id?: string | null;
  detail?: string;
};

function ResultOrError({
  loading,
  status,
  errorDetail,
  result,
}: {
  loading: boolean;
  status: number | null;
  errorDetail: string | null;
  result: UserResponse | null;
}) {
  if (errorDetail) {
    return (
      <p
        role="alert"
        className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300"
      >
        {status ? `HTTP ${status}: ` : ""}
        {errorDetail}
      </p>
    );
  }
  if (result) {
    return (
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        <dt className="text-neutral-500">id</dt>
        <dd>{result.id}</dd>
        <dt className="text-neutral-500">email</dt>
        <dd>{result.email}</dd>
        <dt className="text-neutral-500">is_active</dt>
        <dd>{String(result.is_active)}</dd>
        <dt className="text-neutral-500">role_id</dt>
        <dd>{result.role_id ?? "—"}</dd>
      </dl>
    );
  }
  if (loading) return null;
  return null;
}

export function CreateUserForm() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isActive, setIsActive] = useState(true);
  const [roleId, setRoleId] = useState("");

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<UserResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    const body: Record<string, unknown> = { email, password, is_active: isActive };
    if (roleId.trim()) body.role_id = roleId.trim();

    try {
      const res = await fetch("/api/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await res.json().catch(() => null)) as UserResponse | null;
      setStatus(res.status);
      if (!res.ok) {
        // A 401 means the httpOnly session cookie is gone or dead;
        // redirect to login with a real reason rather than rendering
        // a bare HTTP 401 next to a form that can no longer work (D031).
        if (handleExpiredSession(res.status)) return;
        setErrorDetail(data?.detail ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setResult(data);
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="rounded border border-neutral-300 p-4 dark:border-neutral-700">
      <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        Create user
      </h3>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Email
            <input
              type="email"
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Password
            <input
              type="password"
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Role ID (optional)
            <input
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={roleId}
              onChange={(e) => setRoleId(e.target.value)}
              placeholder="role UUID"
            />
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={isActive}
              onChange={(e) => setIsActive(e.target.checked)}
            />
            Active
          </label>
        </div>
        <button
          type="submit"
          disabled={loading}
          className="self-start rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
        >
          {loading ? "Creating…" : "Create user"}
        </button>
      </form>
      <ResultOrError loading={loading} status={status} errorDetail={errorDetail} result={result} />
    </section>
  );
}

export function UpdateUserForm() {
  const [userId, setUserId] = useState("");
  const [changeActive, setChangeActive] = useState(false);
  const [isActive, setIsActive] = useState(true);
  const [changeRole, setChangeRole] = useState(false);
  const [roleId, setRoleId] = useState("");

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<UserResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    // Only include keys the operator explicitly opted to change — the
    // backend applies model_fields_set semantics: an omitted key is left
    // untouched, but an included "role_id": null unassigns the role.
    const body: Record<string, unknown> = {};
    if (changeActive) body.is_active = isActive;
    if (changeRole) body.role_id = roleId.trim() ? roleId.trim() : null;

    try {
      const res = await fetch(`/api/admin/users/${encodeURIComponent(userId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await res.json().catch(() => null)) as UserResponse | null;
      setStatus(res.status);
      if (!res.ok) {
        // A 401 means the httpOnly session cookie is gone or dead;
        // redirect to login with a real reason rather than rendering
        // a bare HTTP 401 next to a form that can no longer work (D031).
        if (handleExpiredSession(res.status)) return;
        setErrorDetail(data?.detail ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setResult(data);
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="rounded border border-neutral-300 p-4 dark:border-neutral-700">
      <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        Update user
      </h3>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1 text-sm">
          User ID
          <input
            className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            placeholder="user UUID"
            required
          />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={changeActive}
              onChange={(e) => setChangeActive(e.target.checked)}
            />
            Change active status to:
          </label>
          <select
            className="rounded border border-neutral-300 px-3 py-2 text-sm disabled:opacity-50 dark:border-neutral-700 dark:bg-neutral-900"
            value={isActive ? "true" : "false"}
            disabled={!changeActive}
            onChange={(e) => setIsActive(e.target.value === "true")}
          >
            <option value="true">active</option>
            <option value="false">inactive</option>
          </select>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={changeRole}
              onChange={(e) => setChangeRole(e.target.checked)}
            />
            Change role to:
          </label>
          <input
            className="rounded border border-neutral-300 px-3 py-2 text-sm disabled:opacity-50 dark:border-neutral-700 dark:bg-neutral-900"
            value={roleId}
            disabled={!changeRole}
            onChange={(e) => setRoleId(e.target.value)}
            placeholder="role UUID, blank to unassign"
          />
        </div>
        <button
          type="submit"
          disabled={loading}
          className="self-start rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
        >
          {loading ? "Updating…" : "Update user"}
        </button>
      </form>
      <ResultOrError loading={loading} status={status} errorDetail={errorDetail} result={result} />
    </section>
  );
}
