"use client";

import { useState } from "react";
import { handleExpiredSession } from "@/lib/session";

type HistoryPosition = {
  symbol: string;
  quantity: string;
  avg_cost: string;
  current_value: string;
  unrealized_pnl: string;
  realized_pnl: string;
};

type HistoryEntry = {
  id: string;
  broker_id: string;
  captured_at: string;
  cash: string;
  positions: HistoryPosition[];
  total_equity: string;
  total_unrealized_pnl: string;
  total_realized_pnl: string;
};

type HistoryResponse = {
  snapshots?: HistoryEntry[];
  limit?: number;
  offset?: number;
  detail?: string;
};

const WIDTH = 640;
const HEIGHT = 200;
const PAD_LEFT = 8;
const PAD_RIGHT = 8;
const PAD_TOP = 12;
const PAD_BOTTOM = 12;

type Point = { x: number; y: number; entry: HistoryEntry; equity: number };

/**
 * Maps real snapshots to SVG coordinates. Deliberately hand-rolled: a
 * single-series line of `total_equity` over `captured_at` does not
 * justify a new charting dependency, and every value plotted here is a
 * number the backend actually returned — nothing is interpolated,
 * smoothed, back-filled, or extended past the last real snapshot.
 *
 * The x axis is by snapshot index, not elapsed time: D027 capture is
 * manual, so the gaps between snapshots are arbitrary and a time-scaled
 * axis would imply a sampling cadence that does not exist.
 */
export function buildPoints(snapshots: HistoryEntry[]): {
  points: Point[];
  min: number;
  max: number;
} {
  const equities = snapshots.map((s) => Number(s.total_equity));
  const min = Math.min(...equities);
  const max = Math.max(...equities);
  const span = max - min;
  const plotW = WIDTH - PAD_LEFT - PAD_RIGHT;
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM;

  const points = snapshots.map((entry, i) => {
    const equity = equities[i];
    const x =
      snapshots.length === 1
        ? PAD_LEFT + plotW / 2
        : PAD_LEFT + (i / (snapshots.length - 1)) * plotW;
    // A flat series (span 0) is drawn as a centred horizontal line rather
    // than dividing by zero or inventing a slope.
    const ratio = span === 0 ? 0.5 : (equity - min) / span;
    const y = PAD_TOP + (1 - ratio) * plotH;
    return { x, y, entry, equity };
  });

  return { points, min, max };
}

function EquityChart({ snapshots }: { snapshots: HistoryEntry[] }) {
  const { points, min, max } = buildPoints(snapshots);
  const path = points.map((p) => `${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" ");
  const first = snapshots[0];
  const last = snapshots[snapshots.length - 1];

  return (
    <div className="mt-3">
      <svg
        role="img"
        aria-label={`Total equity across ${snapshots.length} captured snapshot${
          snapshots.length === 1 ? "" : "s"
        }`}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="w-full rounded border border-neutral-200 bg-neutral-50 dark:border-neutral-800 dark:bg-neutral-900"
      >
        <polyline
          data-testid="equity-line"
          points={path}
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          className="text-emerald-600 dark:text-emerald-400"
        />
        {points.map((p) => (
          <circle
            key={p.entry.id}
            cx={p.x}
            cy={p.y}
            r={3}
            className="fill-emerald-600 dark:fill-emerald-400"
          >
            <title>{`${p.entry.captured_at} — total_equity ${p.entry.total_equity}`}</title>
          </circle>
        ))}
      </svg>

      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
        <dt className="opacity-70">snapshots</dt>
        <dd>{snapshots.length}</dd>
        <dt className="opacity-70">equity range</dt>
        <dd>
          {min} – {max}
        </dd>
        <dt className="opacity-70">first captured_at</dt>
        <dd>{first.captured_at}</dd>
        <dt className="opacity-70">last captured_at</dt>
        <dd>{last.captured_at}</dd>
      </dl>

      <table className="mt-3 w-full text-left text-xs">
        <caption className="sr-only">
          Every plotted snapshot, exactly as returned by the API
        </caption>
        <thead>
          <tr className="uppercase tracking-wide text-neutral-500">
            <th className="pr-3">captured_at</th>
            <th className="pr-3">total_equity</th>
            <th className="pr-3">cash</th>
            <th className="pr-3">unrealized</th>
            <th className="pr-3">realized</th>
          </tr>
        </thead>
        <tbody>
          {snapshots.map((s) => (
            <tr key={s.id}>
              <td className="pr-3">{s.captured_at}</td>
              <td className="pr-3">{s.total_equity}</td>
              <td className="pr-3">{s.cash}</td>
              <td className="pr-3">{s.total_unrealized_pnl}</td>
              <td className="pr-3">{s.total_realized_pnl}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Historical performance for one broker, on top of Phase 25/D027's
 * persisted snapshots (`GET /brokers/{id}/portfolio/history`).
 *
 * Shows only snapshots that were explicitly POSTed — D027's capture is
 * manual, so an empty result is a real "nobody has captured one yet",
 * rendered as exactly that rather than a zeroed or synthesised curve.
 */
export default function PortfolioHistoryChart() {
  const [brokerId, setBrokerId] = useState("");
  const [limit, setLimit] = useState("50");
  const [offset, setOffset] = useState("0");

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<HistoryResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    const query = new URLSearchParams({ limit, offset });

    try {
      const res = await fetch(
        `/api/portfolio/${encodeURIComponent(brokerId)}/history?${query.toString()}`,
      );
      const data = (await res.json().catch(() => null)) as HistoryResponse | null;
      setStatus(res.status);
      if (!res.ok) {
        // Real backend statuses only — 401 (session gone, handled by the
        // shared redirect), 403 (no VIEW_PORTFOLIO permission or no grant
        // for this broker), 404 (unknown broker), 422 (limit/offset out
        // of the documented bounds). Never a placeholder chart.
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

  const snapshots = result?.snapshots ?? [];

  return (
    <section className="rounded border border-neutral-300 p-4 dark:border-neutral-700">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        Portfolio history
      </h2>
      <p className="mb-3 text-xs text-neutral-500">
        Total equity over time, from persisted snapshots only. Snapshots are
        captured manually — this chart plots exactly the points that were
        captured, in capture order, and nothing between or beyond them.
      </p>

      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
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
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Limit (1–500)
            <input
              type="number"
              min={1}
              max={500}
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={limit}
              onChange={(e) => setLimit(e.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Offset
            <input
              type="number"
              min={0}
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={offset}
              onChange={(e) => setOffset(e.target.value)}
            />
          </label>
        </div>
        <button
          type="submit"
          disabled={loading}
          className="self-start rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
        >
          {loading ? "Loading…" : "Load history"}
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

      {result && !errorDetail && snapshots.length === 0 && (
        <p className="mt-3 text-sm text-neutral-500">
          No snapshots have been captured for this broker yet, so there is no
          history to chart.
        </p>
      )}

      {result && !errorDetail && snapshots.length > 0 && (
        <EquityChart snapshots={snapshots} />
      )}
    </section>
  );
}
