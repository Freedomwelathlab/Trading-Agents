"use client";

import { useState } from "react";
import type { ScoredSignal } from "@/components/PriceChart";
import { EmptyNote, Pill } from "@/components/ui/primitives";

/**
 * The signal-score table that sits above the chart (Phase 85, D102).
 *
 * One row per B/S marker the chart draws, newest first. Hovering (or
 * focusing, for a keyboard) a row opens its score box: the setup's own
 * evidence, the entry it proposed and the stop that invalidates it.
 *
 * The box exists because a marker on a chart invites belief, and these
 * markers have not earned it — every setup behind them measured at or
 * below zero expectancy over 128 sessions (`docs/RESEARCH_5M.md`). Being
 * able to read WHY a marker appeared is the difference between an
 * instrument and an oracle.
 */

function scoreTone(score: number): "pos" | "warn" | "neutral" {
  if (score >= 4) return "pos";
  if (score >= 2) return "warn";
  return "neutral";
}

export default function SignalScoreTable({
  signals,
  note,
  unavailable,
}: {
  signals: ScoredSignal[];
  note?: string;
  unavailable?: string | null;
}) {
  const [open, setOpen] = useState<number | null>(null);

  if (unavailable) {
    return <EmptyNote>{unavailable}</EmptyNote>;
  }
  if (signals.length === 0) {
    return <EmptyNote>No setup fired on these sessions.</EmptyNote>;
  }

  const rows = [...signals].sort(
    (a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime(),
  );

  return (
    <div className="flex flex-col gap-1.5" data-testid="signal-score-table">
      <div className="max-h-44 overflow-y-auto rounded-md border border-line">
        <table className="w-full text-left text-[11px]">
          <thead className="sticky top-0 bg-surface">
            <tr className="border-b border-line uppercase tracking-[0.08em] text-ink-faint">
              <th className="px-2 py-1 font-semibold">time</th>
              <th className="px-2 py-1 font-semibold">B/S</th>
              <th className="px-2 py-1 font-semibold">setup</th>
              <th className="px-2 py-1 font-semibold">entry</th>
              <th className="px-2 py-1 font-semibold">stop</th>
              <th className="px-2 py-1 font-semibold">score</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s, i) => (
              <tr
                key={`${s.ts}-${s.setup}-${i}`}
                tabIndex={0}
                data-testid="signal-row"
                onMouseEnter={() => setOpen(i)}
                onMouseLeave={() => setOpen((cur) => (cur === i ? null : cur))}
                onFocus={() => setOpen(i)}
                onBlur={() => setOpen((cur) => (cur === i ? null : cur))}
                className={
                  "cursor-default border-b border-line/60 font-mono " +
                  (open === i ? "bg-well" : "hover:bg-well")
                }
              >
                <td className="whitespace-nowrap px-2 py-1 text-ink-muted">
                  {new Date(s.ts).toLocaleString(undefined, {
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </td>
                <td className={`px-2 py-1 font-semibold ${s.side === "B" ? "text-pos" : "text-neg"}`}>
                  {s.side}
                </td>
                <td className="px-2 py-1">{s.setup}</td>
                <td className="px-2 py-1 tabular-nums">{Number(s.price).toFixed(2)}</td>
                <td className="px-2 py-1 tabular-nums">{Number(s.stop_price).toFixed(2)}</td>
                <td className="px-2 py-1">
                  <Pill tone={scoreTone(s.score)}>{s.score}</Pill>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {open !== null && rows[open] ? (
        <div
          data-testid="signal-score-box"
          className="rounded-md border border-line bg-well p-2 text-[11px] leading-snug"
        >
          <p className="font-semibold">
            <span className={rows[open].side === "B" ? "text-pos" : "text-neg"}>
              {rows[open].side === "B" ? "BUY" : "SELL"}
            </span>{" "}
            · {rows[open].setup} · score {rows[open].score} ·{" "}
            <span className="font-mono">
              entry {Number(rows[open].price).toFixed(2)} → stop{" "}
              {Number(rows[open].stop_price).toFixed(2)}
            </span>
          </p>
          {Object.entries(rows[open].evidence).length > 0 ? (
            <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-ink-muted">
              {Object.entries(rows[open].evidence).map(([k, v]) => (
                <li key={k}>
                  <span className="text-ink-faint">{k}:</span> {v}
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-1 text-ink-faint">This setup recorded no evidence fields.</p>
          )}
        </div>
      ) : null}

      {note ? <p className="text-[11px] leading-relaxed text-ink-faint">{note}</p> : null}
    </div>
  );
}
