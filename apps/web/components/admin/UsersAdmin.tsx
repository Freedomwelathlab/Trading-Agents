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

type PasswordResetResponse = {
  user_id?: string;
  expires_at?: string;
  delivery?: string;
  reset_link?: string | null;
  detail?: string;
};

/**
 * Issues a real, single-use password reset link for one user
 * (docs/DECISIONS.md D063).
 *
 * A standalone form taking a user ID rather than a per-row button on the
 * `UsersList` table: that table is a read-only listing (D030) and every
 * mutating admin action in this file is already an explicit
 * type-the-id-and-submit form. A one-click button next to every row would
 * also make it very easy to issue a live credential for the wrong account
 * by mis-aiming a click, which is exactly the mistake this action should
 * be awkward enough to prevent.
 *
 * The two delivery outcomes are rendered as what they are, never merged:
 *
 *   NOT_CONFIGURED_returned_directly — the link is shown, labelled as a
 *     live credential the admin must relay themselves. This is the
 *     committed default, so it is the branch most operators will see.
 *   SENT — a confirmation that the backend's email provider ACCEPTED the
 *     message. Not "delivered": nothing in this stack can observe an
 *     inbox, and the copy does not pretend otherwise.
 *
 * A failed send is a real 502 from the backend and renders through the
 * same honest `ResultOrError` path as every other failure here.
 */
export function ResetUserPasswordForm() {
  const [userId, setUserId] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<PasswordResetResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    try {
      const res = await fetch(
        `/api/admin/users/${encodeURIComponent(userId)}/password-reset`,
        { method: "POST" },
      );
      const data = (await res.json().catch(() => null)) as PasswordResetResponse | null;
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
        Issue password reset link
      </h3>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Reset user ID
          <input
            className="w-full rounded-md border border-line bg-well px-3 py-2 text-sm text-ink placeholder:text-ink-faint outline-none transition-colors duration-150 hover:border-line-strong focus:border-accent focus:ring-2 focus:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50"
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            placeholder="user UUID"
            required
          />
        </label>
        <button
          type="submit"
          disabled={loading}
          className="inline-flex cursor-pointer items-center justify-center gap-2 rounded-md text-sm font-semibold transition-all duration-150 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50 bg-accent px-4 py-2 text-accent-ink shadow-sm hover:brightness-110 active:brightness-95 self-start"
        >
          {loading ? "Issuing…" : "Issue reset link"}
        </button>
      </form>

      {errorDetail && (
        <p
          role="alert"
          className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm leading-relaxed text-red-800 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300"
        >
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </p>
      )}

      {result && !errorDetail && (
        <div className="mt-3 flex flex-col gap-2" data-testid="password-reset-result">
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
            <dt className="text-ink-faint">user_id</dt>
            <dd>{result.user_id}</dd>
            <dt className="text-ink-faint">delivery</dt>
            <dd>{result.delivery}</dd>
            <dt className="text-ink-faint">expires_at</dt>
            <dd>{result.expires_at}</dd>
          </dl>

          {result.reset_link ? (
            <>
              <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm leading-relaxed text-amber-900 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300">
                No email provider is configured, so nothing was sent. The link
                below is a live, single-use credential for this account — hand
                it to the user over a channel you trust, and don&apos;t leave it
                on screen.
              </p>
              <code
                data-testid="reset-link"
                className="block break-all rounded-md border border-line bg-well px-3 py-2 font-mono text-xs text-ink"
              >
                {result.reset_link}
              </code>
            </>
          ) : (
            <p className="rounded-md border border-line bg-well px-3 py-2 text-sm leading-relaxed text-ink-muted">
              The configured email provider accepted a message carrying the
              link, so it is not shown here. Accepted is not the same as
              delivered — nothing in this system can see the recipient&apos;s
              inbox.
            </p>
          )}
        </div>
      )}
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
