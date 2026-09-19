"use client";

import { useCallback, useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  Field,
  Panel,
  Pill,
  btnPrimary,
  btnSecondary,
  inputClass,
  monoInputClass,
} from "@/components/ui/primitives";

/**
 * Two admin panels that existed only as API endpoints until Phase 82:
 *
 * - **Emergency stop** (D039): the platform-wide kill switch. Read on
 *   every order submission — manual, agent, deployment and bot. Both
 *   flips require a written reason; the backend refuses a blank one.
 *   It halts; it never liquidates.
 * - **Market-data backfill** (D070): ingest real bars for one symbol over
 *   a date range into this platform's store, synchronously. This is what
 *   makes charts, session levels and backtests possible for a symbol.
 *   The job row that comes back is the vendor's real outcome — a
 *   `failed` status carries the vendor's own message.
 */

type StopStatus = {
  active: boolean;
  source: string;
  reason: string | null;
  actor_user_id: string | null;
  changed_at: string | null;
};

export function EmergencyStopPanel() {
  const [status, setStatus] = useState<StopStatus | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/admin/emergency-stop/status", { cache: "no-store" });
      if (handleExpiredSession(res.status)) return;
      const data = (await res.json().catch(() => null)) as StopStatus | { detail?: string } | null;
      if (!res.ok) {
        setError((data as { detail?: string } | null)?.detail ?? `HTTP ${res.status}`);
        return;
      }
      setStatus(data as StopStatus);
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    }
  }, []);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  async function flip(action: "activate" | "deactivate") {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/admin/emergency-stop/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason }),
      });
      if (handleExpiredSession(res.status)) return;
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      if (!res.ok) {
        const detail = data?.detail;
        setError(
          typeof detail === "string" ? detail : `Request failed (HTTP ${res.status})`,
        );
        return;
      }
      setStatus(data as unknown as StopStatus);
      setReason("");
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title="Emergency stop"
      description="Halts every order path on the platform — manual, agent, strategy deployments and autotrade bots — until deactivated. It never sells anything. A reason is required both ways and is kept in the audit trail."
    >
      <div className="flex flex-col gap-3">
        {error ? <Alert>{error}</Alert> : null}
        {status === null ? (
          <p className="text-sm text-ink-faint">Loading…</p>
        ) : (
          <p className="text-sm">
            <Pill tone={status.active ? "neg" : "pos"} testId="emergency-stop-state">
              {status.active ? "ACTIVE — all trading halted" : "inactive — trading allowed"}
            </Pill>{" "}
            <span className="text-xs text-ink-muted">
              source {status.source}
              {status.changed_at ? ` · changed ${new Date(status.changed_at).toLocaleString()}` : ""}
              {status.reason ? ` · “${status.reason}”` : ""}
            </span>
          </p>
        )}
        <Field label="Reason (required)">
          <input
            id="emergency-stop-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className={inputClass}
            placeholder="e.g. vendor feed misprinting; halting until verified"
          />
        </Field>
        <div className="flex gap-2">
          <button
            type="button"
            className={btnPrimary}
            disabled={busy || !reason.trim() || status?.active === true}
            onClick={() => flip("activate")}
          >
            Activate stop
          </button>
          <button
            type="button"
            className={btnSecondary}
            disabled={busy || !reason.trim() || status?.active !== true}
            onClick={() => flip("deactivate")}
          >
            Deactivate
          </button>
        </div>
      </div>
    </Panel>
  );
}

type Job = {
  id: string;
  symbol: string;
  bar_interval: string;
  requested_start_date: string;
  requested_end_date: string;
  status: string;
  bars_ingested: number;
  earliest_bar_date: string | null;
  latest_bar_date: string | null;
  error_detail: string | null;
};

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

export function MarketDataBackfillPanel() {
  const [symbol, setSymbol] = useState("TQQQ.US");
  const [interval, setInterval] = useState("5m");
  const [start, setStart] = useState(isoDaysAgo(30));
  const [end, setEnd] = useState(isoDaysAgo(0));
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/admin/market-data/backfill", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: symbol.trim().toUpperCase(),
          bar_interval: interval,
          start_date: start,
          end_date: end,
        }),
      });
      if (handleExpiredSession(res.status)) return;
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      if (!res.ok) {
        const detail = data?.detail;
        setError(
          typeof detail === "string"
            ? detail
            : Array.isArray(detail)
              ? JSON.stringify(detail)
              : `Request failed (HTTP ${res.status})`,
        );
        return;
      }
      setJobs((prev) => [data as unknown as Job, ...prev]);
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title="Load market data"
      description="Ingest real bars for a symbol into this platform's store so charts, session levels, backtests and the autotrade bot have history. Runs to completion inside the request — an intraday interval over months takes a minute or more. Needs the LONGPORT_* variables on the server; without them this is a real NOT_CONFIGURED, not a fake job."
    >
      <form onSubmit={run} className="grid grid-cols-1 gap-3 md:grid-cols-4">
        <Field label="Symbol">
          <input id="backfill-symbol" value={symbol} onChange={(e) => setSymbol(e.target.value)} className={monoInputClass} />
        </Field>
        <Field label="Interval">
          <select id="backfill-interval" value={interval} onChange={(e) => setInterval(e.target.value)} className={inputClass}>
            {["1m", "5m", "15m", "30m", "1h", "1d"].map((i) => (
              <option key={i} value={i}>{i}</option>
            ))}
          </select>
        </Field>
        <Field label="From">
          <input id="backfill-start" type="date" value={start} onChange={(e) => setStart(e.target.value)} className={monoInputClass} />
        </Field>
        <Field label="To">
          <input id="backfill-end" type="date" value={end} onChange={(e) => setEnd(e.target.value)} className={monoInputClass} />
        </Field>
        <div className="md:col-span-4">
          <button type="submit" className={btnPrimary} disabled={busy || !symbol.trim()}>
            {busy ? "Loading bars…" : "Load bars"}
          </button>
        </div>
      </form>
      {error ? <Alert className="mt-3">{error}</Alert> : null}
      {jobs.length > 0 ? (
        <ul className="mt-3 flex flex-col gap-1 text-xs">
          {jobs.map((j) => (
            <li key={j.id} className="font-mono">
              <Pill tone={j.status === "succeeded" ? "pos" : j.status === "failed" ? "neg" : "warn"}>{j.status}</Pill>{" "}
              {j.symbol} {j.bar_interval} {j.requested_start_date}→{j.requested_end_date}: {j.bars_ingested} bars
              {j.earliest_bar_date ? ` (${j.earliest_bar_date} … ${j.latest_bar_date})` : ""}
              {j.error_detail ? <span className="text-neg"> — {j.error_detail}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </Panel>
  );
}
