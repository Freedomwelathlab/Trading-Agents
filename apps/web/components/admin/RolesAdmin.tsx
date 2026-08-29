"use client";

import { useState } from "react";
import { handleExpiredSession } from "@/lib/session";

type RoleResponse = {
  id?: string;
  name?: string;
  description?: string | null;
  permissions?: string[];
  detail?: string;
};

function parsePermissions(input: string): string[] {
  return input
    .split(",")
    .map((p) => p.trim())
    .filter((p) => p.length > 0);
}

function ResultOrError({
  status,
  errorDetail,
  result,
}: {
  status: number | null;
  errorDetail: string | null;
  result: RoleResponse | null;
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
        <dt className="text-neutral-500">name</dt>
        <dd>{result.name}</dd>
        <dt className="text-neutral-500">description</dt>
        <dd>{result.description ?? "—"}</dd>
        <dt className="text-neutral-500">permissions</dt>
        <dd>{result.permissions?.join(", ") || "—"}</dd>
      </dl>
    );
  }
  return null;
}

export function CreateRoleForm() {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [permissions, setPermissions] = useState("");

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<RoleResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    const body: Record<string, unknown> = {
      name,
      description: description.trim() ? description.trim() : null,
      permissions: parsePermissions(permissions),
    };

    try {
      const res = await fetch("/api/admin/roles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await res.json().catch(() => null)) as RoleResponse | null;
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
        Create role
      </h3>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Name
          <input
            className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Description (optional)
          <input
            className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Permissions (comma-separated)
          <input
            className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
            value={permissions}
            onChange={(e) => setPermissions(e.target.value)}
            placeholder="trade:submit:paper, admin:manage"
          />
        </label>
        <button
          type="submit"
          disabled={loading}
          className="self-start rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
        >
          {loading ? "Creating…" : "Create role"}
        </button>
      </form>
      <ResultOrError status={status} errorDetail={errorDetail} result={result} />
    </section>
  );
}

export function UpdateRoleForm() {
  const [roleId, setRoleId] = useState("");
  const [changeDescription, setChangeDescription] = useState(false);
  const [description, setDescription] = useState("");
  const [changePermissions, setChangePermissions] = useState(false);
  const [permissions, setPermissions] = useState("");

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<RoleResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    const body: Record<string, unknown> = {};
    if (changeDescription) body.description = description.trim() ? description.trim() : null;
    if (changePermissions) body.permissions = parsePermissions(permissions);

    try {
      const res = await fetch(`/api/admin/roles/${encodeURIComponent(roleId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await res.json().catch(() => null)) as RoleResponse | null;
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
        Update role
      </h3>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Role ID
          <input
            className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
            value={roleId}
            onChange={(e) => setRoleId(e.target.value)}
            placeholder="role UUID"
            required
          />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={changeDescription}
              onChange={(e) => setChangeDescription(e.target.checked)}
            />
            Change description to:
          </label>
          <input
            className="rounded border border-neutral-300 px-3 py-2 text-sm disabled:opacity-50 dark:border-neutral-700 dark:bg-neutral-900"
            value={description}
            disabled={!changeDescription}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={changePermissions}
              onChange={(e) => setChangePermissions(e.target.checked)}
            />
            Replace permissions with:
          </label>
          <input
            className="rounded border border-neutral-300 px-3 py-2 text-sm disabled:opacity-50 dark:border-neutral-700 dark:bg-neutral-900"
            value={permissions}
            disabled={!changePermissions}
            onChange={(e) => setPermissions(e.target.value)}
            placeholder="trade:submit:paper, admin:manage"
          />
        </div>
        <p className="text-xs text-neutral-500">
          Note: role name isn&apos;t updatable via this endpoint (D016).
        </p>
        <button
          type="submit"
          disabled={loading}
          className="self-start rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
        >
          {loading ? "Updating…" : "Update role"}
        </button>
      </form>
      <ResultOrError status={status} errorDetail={errorDetail} result={result} />
    </section>
  );
}
