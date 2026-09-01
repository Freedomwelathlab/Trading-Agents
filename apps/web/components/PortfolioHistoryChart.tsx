"use client";

import { useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import { subscribeToBrokerSelection } from "@/lib/brokerSelection";
import {
  ChartFrame,
  WIDTH,
  HEIGHT,
  PAD_LEFT,
  PAD_RIGHT,
  PAD_TOP,
  PAD_BOTTOM,
} from "@/components/ui/ChartFrame";
import {
  Alert,
  EmptyNote,
  Field,
  Panel,
  TableScroll,
  Term,
  Value,
  btnPrimary,
  inputClass,
  monoInputClass,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";

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

  // Closes the plotted line down to the baseline so the series reads as a
  // filled area. Purely a restatement of the same vertices — it adds no
  // point that `buildPoints` did not already compute from real data.
  const area =
    points.length > 0
      ? `${path} ${points[points.length - 1].x.toFixed(2)},${HEIGHT - PAD_BOTTOM} ` +
        `${points[0].x.toFixed(2)},${HEIGHT - PAD_BOTTOM}`
      : "";

  return (
    <div className="flex flex-col gap-3">
      <div className="rounded-md border border-line bg-well p-3">
        <ChartFrame
          ariaLabel={`Total equity across ${snapshots.length} captured snapshot${
            snapshots.length === 1 ? "" : "s"
          }`}
          min={min}
          max={max}
          testId="equity-line"
          areaPoints={area}
          linePoints={path}
        >
          {points.map((p) => (
            <circle
              key={p.entry.id}
              cx={p.x}
              cy={p.y}
              r={3}
              className="fill-pos stroke-well"
              strokeWidth={1.5}
            >
              <title>{`${p.entry.captured_at} — total_equity ${p.entry.total_equity}`}</title>
            </circle>
          ))}
        </ChartFrame>
      </div>

      <dl className="grid grid-cols-[auto_1fr] items-baseline gap-x-4 gap-y-1.5 text-xs sm:grid-cols-[auto_1fr_auto_1fr]">
        <Term>snapshots</Term>
        <Value>{snapshots.length}</Value>
        <Term>equity range</Term>
        <Value>
          {min} – {max}
        </Value>
        <Term>first captured_at</Term>
        <Value>{first.captured_at}</Value>
        <Term>last captured_at</Term>
        <Value>{last.captured_at}</Value>
      </dl>

      <TableScroll>
        <table className={tableClass}>
          <caption className="sr-only">
            Every plotted snapshot, exactly as returned by the API
          </caption>
          <thead>
            <tr className={theadRowClass}>
              <th className={thClass}>captured_at</th>
              <th className={thClass}>total_equity</th>
              <th className={thClass}>cash</th>
              <th className={thClass}>unrealized</th>
              <th className={thClass}>realized</th>
            </tr>
          </thead>
          <tbody>
            {snapshots.map((s) => (
              <tr key={s.id} className={tbodyRowClass}>
                <td className={`${tdClass} text-ink-muted`}>{s.captured_at}</td>
                <td className={`${tdClass} font-semibold`}>{s.total_equity}</td>
                <td className={tdClass}>{s.cash}</td>
                <td className={tdClass}>{s.total_unrealized_pnl}</td>
                <td className={tdClass}>{s.total_realized_pnl}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
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
export default function PortfolioHistoryChart({ className }: { className?: string } = {}) {
  const [brokerId, setBrokerId] = useState("");

  // D034: the broker discovery list can push a real, granted broker id
  // here so the user never has to paste a UUID. Pre-filling authorizes
  // nothing - the backend re-checks the grant on submit.
  useEffect(() => subscribeToBrokerSelection(setBrokerId), []);
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
    <Panel
      className={className}
      title="Portfolio history"
      description="Total equity over time, from persisted snapshots only. Snapshots are captured manually — this chart plots exactly the points that were captured, in capture order, and nothing between or beyond them."
    >
      <form
        onSubmit={handleSubmit}
        className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_7rem_7rem_auto] sm:items-end"
      >
        <Field label="Broker ID">
          <input
            className={monoInputClass}
            value={brokerId}
            onChange={(e) => setBrokerId(e.target.value)}
            placeholder="broker UUID"
            required
          />
        </Field>
        <Field label="Limit (1–500)">
          <input
            type="number"
            min={1}
            max={500}
            className={inputClass}
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
          />
        </Field>
        <Field label="Offset">
          <input
            type="number"
            min={0}
            className={inputClass}
            value={offset}
            onChange={(e) => setOffset(e.target.value)}
          />
        </Field>
        <button type="submit" disabled={loading} className={btnPrimary}>
          {loading ? "Loading…" : "Load history"}
        </button>
      </form>

      {errorDetail && (
        <Alert tone="error">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </Alert>
      )}

      {result && !errorDetail && snapshots.length === 0 && (
        <EmptyNote>
          No snapshots have been captured for this broker yet, so there is no
          history to chart.
        </EmptyNote>
      )}

      {result && !errorDetail && snapshots.length > 0 && (
        <EquityChart snapshots={snapshots} />
      )}
    </Panel>
  );
}
