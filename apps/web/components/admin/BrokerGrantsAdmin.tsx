"use client";

import { useState } from "react";

type GrantResponse = {
  id?: string;
  user_id?: string;
  broker_id?: string;
  detail?: string;
};

export function CreateBrokerGrantForm() {
  const [userId, setUserId] = useState("");
  const [brokerId, setBrokerId] = useState("");

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<GrantResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    try {
      const res = await fetch("/api/admin/broker-grants", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId, broker_id: brokerId }),
      });
      const data = (await res.json().catch(() => null)) as GrantResponse | null;
      setStatus(res.status);
      if (!res.ok) {
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
        Grant broker access
      </h3>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-3">
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
          <label className="flex flex-col gap-1 text-sm">
            Broker ID
            <input
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={brokerId}
              onChange={(e) => setBrokerId(e.target.value)}
              placeholder="broker UUID"
              required
            />
          </label>
        </div>
        <button
          type="submit"
          disabled={loading}
          className="self-start rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
        >
          {loading ? "Granting…" : "Create grant"}
        </button>
      </form>

      {errorDetail && (
        <p
          role="alert"
          className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300"
        >
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </p>
      )}
      {result && !errorDetail && (
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          <dt className="text-neutral-500">id</dt>
          <dd>{result.id}</dd>
          <dt className="text-neutral-500">user_id</dt>
          <dd>{result.user_id}</dd>
          <dt className="text-neutral-500">broker_id</dt>
          <dd>{result.broker_id}</dd>
        </dl>
      )}
    </section>
  );
}

export function DeleteBrokerGrantForm() {
  const [grantId, setGrantId] = useState("");

  const [loading, setLoading] = useState(false);
  const [deleted, setDeleted] = useState(false);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setDeleted(false);
    setErrorDetail(null);
    setStatus(null);

    try {
      const res = await fetch(`/api/admin/broker-grants/${encodeURIComponent(grantId)}`, {
        method: "DELETE",
      });
      setStatus(res.status);
      if (res.status === 204) {
        setDeleted(true);
        return;
      }
      const data = (await res.json().catch(() => null)) as { detail?: string } | null;
      setErrorDetail(data?.detail ?? `Request failed (HTTP ${res.status})`);
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="rounded border border-neutral-300 p-4 dark:border-neutral-700">
      <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        Revoke broker access
      </h3>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Grant ID
          <input
            className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
            value={grantId}
            onChange={(e) => setGrantId(e.target.value)}
            placeholder="grant UUID"
            required
          />
        </label>
        <button
          type="submit"
          disabled={loading}
          className="self-start rounded bg-red-700 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {loading ? "Revoking…" : "Delete grant"}
        </button>
      </form>

      {errorDetail && (
        <p
          role="alert"
          className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300"
        >
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </p>
      )}
      {deleted && (
        <p className="mt-3 rounded bg-green-50 px-3 py-2 text-sm text-green-800 dark:bg-green-950 dark:text-green-300">
          Grant revoked (204 No Content).
        </p>
      )}
    </section>
  );
}
