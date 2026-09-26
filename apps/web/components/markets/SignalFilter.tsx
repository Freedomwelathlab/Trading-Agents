"use client";

import { useState } from "react";
import { btnGhost, btnSecondary } from "@/components/ui/primitives";

/**
 * The signal filter beside the Indicators tab (Phase 98, D117).
 *
 * The operator asked for markers only when a signal scores 7+ AND is at
 * least 70% likely to win, with tick boxes to choose which scores appear.
 * Those two defaults are exactly what this ships with. They are applied by
 * the backend, which also reports how many signals fired before filtering
 * - so an empty chart says "nothing met your bar", not "nothing happened".
 *
 * Confidence is MEASURED, not asserted: the win rate of the same setup,
 * direction and score over the last 60 sessions (+1R before the stop).
 */

export type SignalFilterValue = {
  scores: number[];
  minConfidence: number;
};

export const DEFAULT_SIGNAL_FILTER: SignalFilterValue = {
  scores: [7, 8, 9, 10],
  minConfidence: 70,
};

const ALL_SCORES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
const CONFIDENCE_STEPS = [0, 50, 55, 60, 65, 70, 75, 80];

export default function SignalFilter({
  value,
  onChange,
}: {
  value: SignalFilterValue;
  onChange: (next: SignalFilterValue) => void;
}) {
  const [open, setOpen] = useState(false);

  function toggle(score: number) {
    const has = value.scores.includes(score);
    const scores = has ? value.scores.filter((s) => s !== score) : [...value.scores, score];
    onChange({ ...value, scores: scores.sort((a, b) => a - b) });
  }

  const summary =
    value.scores.length === ALL_SCORES.length
      ? "all scores"
      : value.scores.length === 0
        ? "no scores"
        : `score ${value.scores.join(",")}`;

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className={open ? btnSecondary : btnGhost}
        data-testid="signal-filter-button"
      >
        Signals: {summary} · ≥{value.minConfidence}%
      </button>
      {open ? (
        <div
          className="absolute right-0 z-20 mt-1 w-72 rounded-md border border-line bg-surface p-3 text-xs shadow-lg"
          role="dialog"
          aria-label="Signal filter"
        >
          <p className="mb-1 font-medium">Show signals with score</p>
          <div className="grid grid-cols-5 gap-1.5">
            {ALL_SCORES.map((s) => (
              <label key={s} className="flex cursor-pointer items-center gap-1">
                <input
                  type="checkbox"
                  checked={value.scores.includes(s)}
                  onChange={() => toggle(s)}
                  aria-label={`Score ${s}`}
                />
                {s}
              </label>
            ))}
          </div>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              className={btnGhost}
              onClick={() => onChange({ ...value, scores: [...ALL_SCORES] })}
            >
              All
            </button>
            <button
              type="button"
              className={btnGhost}
              onClick={() => onChange({ ...DEFAULT_SIGNAL_FILTER })}
            >
              Reset to 7+ / 70%
            </button>
          </div>
          <label className="mt-3 flex items-center justify-between gap-2">
            <span className="font-medium">Minimum confidence</span>
            <select
              value={value.minConfidence}
              onChange={(e) => onChange({ ...value, minConfidence: Number(e.target.value) })}
              aria-label="Minimum confidence"
              className="rounded border border-line bg-surface px-1.5 py-0.5"
            >
              {CONFIDENCE_STEPS.map((c) => (
                <option key={c} value={c}>
                  {c === 0 ? "any" : `${c}%`}
                </option>
              ))}
            </select>
          </label>
          <p className="mt-2 text-[11px] leading-snug text-ink-faint">
            Confidence is the measured win rate of the same setup, direction and score over the last
            60 sessions: a win is one risk-unit in profit before the stop. Buckets with fewer than 10
            resolved signals have no confidence and are hidden when a minimum is set.
          </p>
        </div>
      ) : null}
    </div>
  );
}
