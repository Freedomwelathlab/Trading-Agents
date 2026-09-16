"use client";

import Link from "next/link";
import { Fragment, useEffect, useState, type JSX } from "react";
import { handleExpiredSession } from "@/lib/session";
import { formatDetail } from "@/components/StrategyList";
import {
  Alert,
  EmptyNote,
  Field,
  Panel,
  Pill,
  TableScroll,
  btnGhost,
  inputClass,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";

export type ScoreComponent = {
  name: string;
  points: string;
  max_points: string;
  detail: string;
};

export type StrategyScoreStatus =
  | "insufficient_data"
  | "promising"
  | "validated"
  | "overfit_risk";

export type StrategyScore = {
  strategy_version_id: string;
  components: ScoreComponent[];
  total_points: string;
  max_possible_points: string;
  percentage: string;
  components_measured: number;
  status: StrategyScoreStatus;
  status_reason: string;
  latest_backtest_run_id: string | null;
  latest_walk_forward_run_id: string | null;
  latest_monte_carlo_run_id: string | null;
  latest_robustness_run_id: string | null;
};

export type LeaderboardItem = {
  strategy_id: string;
  strategy_name: string;
  strategy_version_id: string;
  strategy_version_number: number;
  score: StrategyScore;
};

export type LeaderboardResponse = {
  items?: LeaderboardItem[];
  limit?: number;
  offset?: number;
  detail?: unknown;
};

/**
 * Total number of scoring categories the backend can ever measure
 * (`return`, `risk`, `consistency`, `parameter_stability` — see the
 * `score.components` shape in docs/API.md). A given item's `components`
 * array only ever contains the ones actually measured for that strategy,
 * so "components_measured" is rendered against this fixed universe rather
 * than against `components.length` a second time.
 */
const TOTAL_SCORE_COMPONENTS = 4;

const STATUS_TONE: Record<StrategyScoreStatus, "pos" | "neg" | "warn" | "neutral"> = {
  validated: "pos",
  // "Promising" is a cautiously-positive read — some evidence the strategy
  // works, not yet enough to call it validated — so it gets the amber
  // "warn" tone rather than the same green as `validated`.
  promising: "warn",
  overfit_risk: "neg",
  insufficient_data: "neutral",
};

type MinStatusFilter = "all" | "promising" | "validated";

function scoreText(raw: string): string {
  const n = Number(raw);
  return Number.isFinite(n) ? `${n.toFixed(2)}%` : `${raw}%`;
}

/**
 * Strategy leaderboard (Phase 59), read-only.
 *
 * Fetches `GET /api/strategies/leaderboard` on mount and whenever the
 * `min_status` filter changes. The backend already returns items sorted by
 * `percentage` descending — this component never re-sorts them, so "Rank"
 * is always just the item's 1-indexed position in the array the backend
 * sent.
 *
 * Every numeric field on `score` arrives as a JSON string (the backend
 * serializes `Decimal` as strings). They are parsed with `Number(...)`
 * only where a computation or color decision needs a number — the raw
 * string is what actually renders, so no float rounding ever creeps into
 * a number a reviewer might reconcile against the backend's own log.
 *
 * The score percentage is rendered as plain text, not tinted with the
 * app's pnl-tone (green/red) convention: `pctClass` in
 * `BacktestRunList.tsx` colors a P&L figure by the sign of a return, but
 * a score's percentage isn't signed and isn't a return — coloring it green
 * or red would imply a stronger read ("profit" / "loss") than "how well
 * measured is this strategy" actually means. The `status` Pill already
 * carries that read explicitly.
 */
export function StrategyLeaderboard(): JSX.Element {
  const [items, setItems] = useState<LeaderboardItem[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);
  const [minStatus, setMinStatus] = useState<MinStatusFilter>("all");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  async function load(filter: MinStatusFilter) {
    setLoading(true);
    setErrorDetail(null);
    setStatus(null);
    try {
      const query = filter === "all" ? "" : `?min_status=${encodeURIComponent(filter)}`;
      const res = await fetch(`/api/strategies/leaderboard${query}`, { cache: "no-store" });
      const data = (await res.json().catch(() => null)) as LeaderboardResponse | null;
      setStatus(res.status);
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setItems(null);
        setErrorDetail(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setItems(data?.items ?? []);
    } catch {
      setItems(null);
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // Deferred by one microtask so the loader's first `setState` lands
    // AFTER this effect returns rather than during it - calling it
    // synchronously re-renders from inside the effect React is still
    // committing, which `react-hooks` flags as a cascading render.
    const status = minStatus;
    void Promise.resolve().then(() => load(status));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [minStatus]);

  function toggleExpanded(versionId: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(versionId)) next.delete(versionId);
      else next.add(versionId);
      return next;
    });
  }

  const emptyMessage =
    minStatus === "all"
      ? "No strategy has a completed backtest yet."
      : "No strategies currently meet this bar.";

  return (
    <Panel
      title="Leaderboard"
      description="Every strategy version the scorer has measured, ranked by score. A component only appears in a row's breakdown once it has actually been measured — nothing here is padded to look complete."
    >
      <Field label="Minimum status" className="max-w-xs">
        <select
          className={inputClass}
          value={minStatus}
          onChange={(e) => setMinStatus(e.target.value as MinStatusFilter)}
        >
          <option value="all">All</option>
          <option value="promising">Promising</option>
          <option value="validated">Validated</option>
        </select>
      </Field>

      {errorDetail && (
        <Alert tone="error">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </Alert>
      )}

      {loading && items === null && !errorDetail && (
        <EmptyNote>Loading leaderboard…</EmptyNote>
      )}

      {items !== null && !errorDetail && items.length === 0 && (
        <EmptyNote>{emptyMessage}</EmptyNote>
      )}

      {items !== null && !errorDetail && items.length > 0 && (
        <TableScroll>
          <table className={tableClass}>
            <caption className="sr-only">
              Strategy leaderboard, exactly as returned by the API
            </caption>
            <thead>
              <tr className={theadRowClass}>
                <th className={thClass}>#</th>
                <th className={thClass}>Strategy</th>
                <th className={thClass}>Version</th>
                <th className={thClass}>Status</th>
                <th className={thClass}>Score</th>
                <th className={thClass}>Components measured</th>
                <th className={thClass}>
                  <span className="sr-only">Details</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => {
                const isOpen = expanded.has(item.strategy_version_id);
                return (
                  <Fragment key={item.strategy_version_id}>
                    <tr
                      className={tbodyRowClass}
                      data-testid="leaderboard-row"
                    >
                      <td className={tdClass}>{index + 1}</td>
                      <td className={tdClass}>
                        <Link
                          href={`/strategies/${item.strategy_id}`}
                          className="font-semibold text-ink underline-offset-2 hover:text-accent hover:underline"
                        >
                          {item.strategy_name}
                        </Link>
                      </td>
                      <td className={tdClass}>v{item.strategy_version_number}</td>
                      <td className={tdClass}>
                        <Pill tone={STATUS_TONE[item.score.status]}>{item.score.status}</Pill>
                      </td>
                      <td className={tdClass} data-testid={`score-${item.strategy_version_id}`}>
                        {scoreText(item.score.percentage)}
                      </td>
                      <td className={tdClass}>
                        {item.score.components_measured}/{TOTAL_SCORE_COMPONENTS}
                      </td>
                      <td className={tdClass}>
                        <button
                          type="button"
                          className={btnGhost}
                          onClick={() => toggleExpanded(item.strategy_version_id)}
                          aria-expanded={isOpen}
                        >
                          {isOpen ? "Hide breakdown" : "Show breakdown"}
                        </button>
                      </td>
                    </tr>
                    {isOpen && (
                      <tr
                        className={tbodyRowClass}
                        data-testid={`leaderboard-detail-${item.strategy_version_id}`}
                      >
                        <td className={tdClass} />
                        <td className={`${tdClass} whitespace-normal`} colSpan={6}>
                          <p className="mb-2 font-mono text-[11px] text-ink-muted">
                            {item.score.status_reason}
                          </p>
                          <ul className="flex flex-col gap-1.5">
                            {item.score.components.map((c) => (
                              <li key={c.name} className="whitespace-normal">
                                <span className="font-semibold text-ink">{c.name}</span>
                                {": "}
                                <span className="tnum">
                                  {c.points}/{c.max_points}
                                </span>
                                {" — "}
                                <span className="text-ink-muted">{c.detail}</span>
                              </li>
                            ))}
                          </ul>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </TableScroll>
      )}
    </Panel>
  );
}

export default StrategyLeaderboard;
