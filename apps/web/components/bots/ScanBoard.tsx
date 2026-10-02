"use client";

import { Pill, TableScroll, tableClass, tbodyRowClass, tdClass, thClass, theadRowClass } from "@/components/ui/primitives";
import type { SetupScan, SymbolBoard } from "./types";

/**
 * One symbol's scan: the recommendation it adds up to, the trade it
 * proposes, and every setup's score on the latest bar. Shared by the
 * crypto, forex and options dashboards (Phase 107, D133).
 */

const REC_TONE = { BUY: "pos", SELL: "neg", WAIT: "neutral" } as const;

const VERDICT: Record<SetupScan["verdict"], { label: string; tone: "pos" | "warn" | "neutral" | "neg" }> = {
  qualifies: { label: "would trade", tone: "pos" },
  below_min_score: { label: "score too low", tone: "warn" },
  direction_not_allowed: { label: "side not allowed", tone: "warn" },
  stop_quality: { label: "stop rejected", tone: "warn" },
  not_enabled: { label: "setup off", tone: "neutral" },
  no_signal: { label: "no signal", tone: "neutral" },
};

/** A 0-10 score as ten cells, filled to the score, with a tick at the bot's minimum. */
export function ScoreMeter({ score, min }: { score: number | null; min?: number }) {
  const s = score ?? 0;
  return (
    <span
      className="inline-flex items-center gap-[2px] align-middle"
      role="meter"
      aria-valuemin={0}
      aria-valuemax={10}
      aria-valuenow={s}
      aria-label={`score ${s} of 10`}
    >
      {Array.from({ length: 10 }, (_, i) => {
        const filled = score !== null && i < s;
        const atMin = min !== undefined && i === min - 1;
        return (
          <span
            key={i}
            className={`h-2.5 w-1.5 rounded-[1px] ${filled ? (s >= (min ?? 11) ? "bg-pos" : "bg-amber-500") : "bg-line"} ${atMin ? "outline outline-1 outline-ink-faint" : ""}`}
          />
        );
      })}
      <span className="ml-1.5 font-mono text-[11px] text-ink-muted">{score ?? "—"}</span>
    </span>
  );
}

function fmt(v: string | null, digits = 5): string {
  if (v === null) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return v;
  return n.toLocaleString(undefined, { maximumFractionDigits: n >= 100 ? 2 : digits });
}

export default function ScanBoard({ board, minScore }: { board: SymbolBoard; minScore: number }) {
  const best = board.best;
  const fired = board.setups.filter((s) => s.fired).length;
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className="font-mono text-sm font-semibold text-ink">{board.symbol}</span>
          <Pill tone={REC_TONE[board.recommendation]}>{board.recommendation}</Pill>
          {board.phase ? <span className="text-xs text-ink-faint">{board.phase}</span> : null}
        </div>
        <span className="font-mono text-xs text-ink-faint">
          last {fmt(board.last_close)}
          {board.last_ts ? ` · ${new Date(board.last_ts).toLocaleString()}` : ""}
        </span>
      </div>
      <p className="text-sm text-ink">{board.headline}</p>

      {best && best.fired ? (
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 rounded-md bg-well p-3 text-xs sm:grid-cols-4">
          <div><div className="text-ink-faint">best setup</div><div className="font-mono text-ink">{best.setup} · {best.direction}</div></div>
          <div><div className="text-ink-faint">scan score</div><ScoreMeter score={best.score} min={minScore} /></div>
          <div><div className="text-ink-faint">entry / stop</div><div className="font-mono text-ink">{fmt(best.entry)} / {fmt(best.stop)}</div></div>
          <div><div className="text-ink-faint">1R target · risk</div><div className="font-mono text-ink">{fmt(best.target_1r)} · {best.risk_pct ?? "—"}%</div></div>
          <div className="col-span-2 sm:col-span-4">
            <span className="text-ink-faint">measured confidence: </span>
            <span className="font-mono text-ink">
              {best.confidence_pct ? `${best.confidence_pct}% won +1R before the stop, ${best.sample} past signals` : board.calibration === "ready" ? "too few past signals to measure (needs 10)" : board.calibration}
            </span>
          </div>
        </div>
      ) : null}

      <TableScroll>
        <table className={tableClass}>
          <thead>
            <tr className={theadRowClass}>
              <th className={thClass}>setup</th>
              <th className={thClass}>side</th>
              <th className={thClass}>scan score</th>
              <th className={thClass}>confidence</th>
              <th className={thClass}>entry</th>
              <th className={thClass}>stop</th>
              <th className={thClass}>1R target</th>
              <th className={thClass}>verdict</th>
            </tr>
          </thead>
          <tbody>
            {board.setups.map((s) => (
              <tr key={s.setup} className={`${tbodyRowClass} ${s.fired ? "" : "opacity-60"}`}>
                <td className={tdClass}>{s.setup}{s.enabled ? "" : " ·off"}</td>
                <td className={tdClass}>{s.direction ?? "—"}</td>
                <td className={tdClass}>{s.fired ? <ScoreMeter score={s.score} min={minScore} /> : "—"}</td>
                <td className={tdClass}>{s.confidence_pct ? `${s.confidence_pct}% (${s.sample})` : "—"}</td>
                <td className={tdClass}>{fmt(s.entry)}</td>
                <td className={tdClass}>{fmt(s.stop)}</td>
                <td className={tdClass}>{fmt(s.target_1r)}</td>
                <td className={tdClass}><Pill tone={VERDICT[s.verdict].tone}>{VERDICT[s.verdict].label}</Pill></td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
      <p className="text-xs text-ink-faint">
        {fired} of {board.setups.length} setups fire on the latest bar · {board.bars_used} bars read · minimum score {minScore}
        {board.calibration !== "ready" ? ` · confidence ${board.calibration}` : ""}
      </p>
    </div>
  );
}
