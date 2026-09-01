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
        className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm leading-relaxed text-red-800 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300"
      >
        {status ? `HTTP ${status}: ` : ""}
        {errorDetail}
      </p>
    );
  }
  if (result) {
    return (
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        <dt className="text-ink-faint">id</dt>
        <dd>{result.id}</dd>
        <dt className="text-ink-faint">email</dt>
        <dd>{result.email}</dd>
        <dt className="text-ink-faint">is_active</dt>
        <dd>{String(result.is_active)}</dd>
        <dt className="text-ink-faint">role_id</dt>
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
    <section className="flex flex-col overflow-hidden rounded-lg border border-line bg-surface p-4 shadow-[var(--shadow-panel)]">
      <h3 className="mb-3 text-sm font-semibold tracking-tight text-ink">
        Create user
      </h3>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Email
            <input
              type="email"
              className="w-full rounded-md border border-line bg-well px-3 py-2 text-sm text-ink placeholder:text-ink-faint outline-none transition-colors duration-150 hover:border-line-strong focus:border-accent focus:ring-2 focus:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Password
            <input
              type="password"
              className="w-full rounded-md border border-line bg-well px-3 py-2 text-sm text-ink placeholder:text-ink-faint outline-none transition-colors duration-150 hover:border-line-strong focus:border-accent focus:ring-2 focus:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50"
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
              className="w-full rounded-md border border-line bg-well px-3 py-2 text-sm text-ink placeholder:text-ink-faint outline-none transition-colors duration-150 hover:border-line-strong focus:border-accent focus:ring-2 focus:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50"
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
          className="inline-flex cursor-pointer items-center justify-center gap-2 rounded-md text-sm font-semibold transition-all duration-150 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50 bg-accent px-4 py-2 text-accent-ink shadow-sm hover:brightness-110 active:brightness-95 self-start"
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
    <section className="flex flex-col overflow-hidden rounded-lg border border-line bg-surface p-4 shadow-[var(--shadow-panel)]">
      <h3 className="mb-3 text-sm font-semibold tracking-tight text-ink">
        Update user
      </h3>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1 text-sm">
          User ID
          <input
            className="w-full rounded-md border border-line bg-well px-3 py-2 text-sm text-ink placeholder:text-ink-faint outline-none transition-colors duration-150 hover:border-line-strong focus:border-accent focus:ring-2 focus:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50"
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
            className="w-full rounded-md border border-line bg-well px-3 py-2 text-sm text-ink placeholder:text-ink-faint outline-none transition-colors duration-150 hover:border-line-strong focus:border-accent focus:ring-2 focus:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50"
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
            className="w-full rounded-md border border-line bg-well px-3 py-2 text-sm text-ink placeholder:text-ink-faint outline-none transition-colors duration-150 hover:border-line-strong focus:border-accent focus:ring-2 focus:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50"
            value={roleId}
            disabled={!changeRole}
            onChange={(e) => setRoleId(e.target.value)}
            placeholder="role UUID, blank to unassign"
          />
        </div>
        <button
          type="submit"
          disabled={loading}
          className="inline-flex cursor-pointer items-center justify-center gap-2 rounded-md text-sm font-semibold transition-all duration-150 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50 bg-accent px-4 py-2 text-accent-ink shadow-sm hover:brightness-110 active:brightness-95 self-start"
        >
          {loading ? "Updating…" : "Update user"}
        </button>
      </form>
      <ResultOrError loading={loading} status={status} errorDetail={errorDetail} result={result} />
    </section>
  );
}
