"use client";

import { useMemo } from "react";
import { Alert, EmptyNote, Panel, Pill } from "@/components/ui/primitives";
import { classifyWithAbsences, useKeyedFetch } from "@/lib/useKeyedFetch";
import type { PriceLine } from "@/components/PriceChart";

/**
 * Session-anchored levels for the latest stored session (Phase 74, D092).
 *
 * These are the locations an intraday chart is actually read against —
 * the previous day's high and low, the premarket extremes, the opening
 * range, session VWAP and its 2σ bands. They are derived from this
 * platform's own stored bars rather than requested from a vendor, because
 * no vendor publishes them: they are facts about where each bar sits on
 * the exchange's clock.
 *
 * **A null level is rendered as an absence, never as a zero, and never
 * omitted silently.** The row stays on screen with an explicit dash, so a
 * reader can tell "this session had no premarket trading" apart from
 * "this panel forgot about premarket". A zero previous-low would sit
 * below every candle and read as a level price never reached.
 *
 * The parent lifts these into chart price lines via `toPriceLines`, so the
 * numbers in this table and the lines on the chart are the same values
 * from the same response — they cannot drift apart.
 */

export type SessionLevels = {
  symbol: string;
  bar_interval: string;
  session_date: string;
  previous_high: string | null;
  previous_low: string | null;
  previous_close: string | null;
  premarket_high: string | null;
  premarket_low: string | null;
  opening_range_high: string | null;
  opening_range_low: string | null;
  regular_open: string | null;
  vwap: string | null;
  vwap_upper_2sigma: string | null;
  vwap_lower_2sigma: string | null;
  bars_in_session: number;
};

/** The rows, in the order a trader marks them before a session. */
const ROWS: { key: keyof SessionLevels; label: string; tone: PriceLine["tone"] }[] = [
  { key: "previous_high", label: "Prev day high", tone: "prior" },
  { key: "previous_low", label: "Prev day low", tone: "prior" },
  { key: "previous_close", label: "Prev day close", tone: "prior" },
  { key: "premarket_high", label: "Premarket high", tone: "premarket" },
  { key: "premarket_low", label: "Premarket low", tone: "premarket" },
  { key: "opening_range_high", label: "Opening range high", tone: "opening" },
  { key: "opening_range_low", label: "Opening range low", tone: "opening" },
  { key: "vwap", label: "Session VWAP", tone: "vwap" },
  { key: "vwap_upper_2sigma", label: "VWAP +2σ", tone: "band" },
  { key: "vwap_lower_2sigma", label: "VWAP −2σ", tone: "band" },
];

/** Only levels the backend actually reported become chart lines. */
export function toPriceLines(levels: SessionLevels | null): PriceLine[] {
  if (!levels) return [];
  const lines: PriceLine[] = [];
  for (const row of ROWS) {
    const value = levels[row.key];
    if (typeof value === "string" && value.length > 0) {
      lines.push({ price: value, label: row.label, tone: row.tone });
    }
  }
  return lines;
}

export function useSessionLevels(symbol: string, barInterval: string) {
  const classify = useMemo(
    // 404 means the store holds no bars for this symbol yet — a gap to
    // backfill, not a failure. It is shown as an absence with the
    // backend's own instruction, never as a red error.
    () => classifyWithAbsences<SessionLevels>([404], "No stored bars for this symbol."),
    [],
  );

  const { data, unavailable, error, reload } = useKeyedFetch<SessionLevels>({
    key: `${symbol}|${barInterval}`,
    url: `/api/market-data/${encodeURIComponent(symbol)}/session-levels?bar_interval=${encodeURIComponent(barInterval)}`,
    classify,
  });

  return { levels: data, unavailable, error, reload };
}


export default function SessionLevelsPanel({
  levels,
  unavailable,
  error,
  className,
}: {
  levels: SessionLevels | null;
  unavailable: string | null;
  error: string | null;
  className?: string;
}) {
  return (
    <Panel
      className={className}
      title="Session levels"
      description={
        levels
          ? `${levels.session_date} · ${levels.bars_in_session} bars · ${levels.bar_interval}`
          : "Derived from stored bars"
      }
    >
      {error ? <Alert>{error}</Alert> : null}

      {unavailable ? (
        <div className="flex flex-col gap-2">
          <Pill tone="neutral">DATA_UNAVAILABLE</Pill>
          <p className="text-xs text-ink-faint leading-relaxed">{unavailable}</p>
        </div>
      ) : null}

      {!levels && !unavailable && !error ? <EmptyNote>Loading levels…</EmptyNote> : null}

      {levels ? (
        <ul className="flex flex-col gap-px text-xs">
          {ROWS.map((row) => {
            const value = levels[row.key];
            const shown = typeof value === "string" && value.length > 0;
            return (
              <li
                key={row.key}
                className="flex items-center justify-between px-2 py-1.5 odd:bg-well rounded-sm"
              >
                <span className="flex items-center gap-2">
                  <span
                    aria-hidden
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ background: toneColor(row.tone) }}
                  />
                  <span className="text-ink-muted">{row.label}</span>
                </span>
                <span className="font-mono tabular-nums">
                  {shown ? (
                    value
                  ) : (
                    /* An explicit dash, not a hidden row: the reader can
                       tell "this session had none" from "this panel
                       forgot about it". */
                    <span className="text-ink-faint" title="Not supported by the stored bars">
                      —
                    </span>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      ) : null}
    </Panel>
  );
}

function toneColor(tone: PriceLine["tone"]): string {
  switch (tone) {
    case "prior":
      return "var(--ink-faint)";
    case "premarket":
      return "var(--accent)";
    case "opening":
      return "var(--level-opening)";
    case "vwap":
      return "var(--level-vwap)";
    case "band":
      return "var(--grid)";
  }
}
