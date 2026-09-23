"use client";

import { useMemo } from "react";
import { classifyWithAbsences, useKeyedFetch } from "@/lib/useKeyedFetch";
import { Alert, EmptyNote, Panel, Pill } from "@/components/ui/primitives";

/**
 * Pre-market / regular / after-hours movement (Phase 86, D105).
 *
 * Three rows, each showing what a session phase actually did and — this
 * is the part that stops the percentage being a guess — what it was
 * measured against. A quoted "+2.1% pre-market" is meaningless without
 * knowing whether that is against yesterday's close or against the
 * pre-market open, and the two differ by the overnight gap.
 *
 * A phase with no bars is ABSENT, not flat. Nothing traded and traded
 * unchanged are different facts, and a row of zeros would say the second.
 */

type PhaseMove = {
  phase: string;
  bars: number;
  first: string;
  last: string;
  high: string;
  low: string;
  volume: number | null;
  reference: string | null;
  reference_label: string;
  change: string | null;
  change_pct: string | null;
};

type ExtendedHours = {
  symbol: string;
  bar_interval: string;
  session_date: string;
  pre_market: PhaseMove | null;
  regular: PhaseMove | null;
  after_hours: PhaseMove | null;
  note: string;
};

const LABELS: [keyof ExtendedHours, string][] = [
  ["pre_market", "Pre-market"],
  ["regular", "Regular"],
  ["after_hours", "After hours"],
];

function num(v: string | null | undefined, digits = 2): string {
  if (v == null) return "—";
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digits) : "—";
}

function Row({ label, move }: { label: string; move: PhaseMove | null }) {
  if (!move) {
    return (
      <tr className="border-b border-line/60 last:border-b-0">
        <td className="px-2 py-1.5 font-medium">{label}</td>
        <td className="px-2 py-1.5 text-ink-faint" colSpan={4}>
          No bars stored in this phase.
        </td>
      </tr>
    );
  }
  const pct = move.change_pct == null ? null : Number(move.change_pct);
  const tone = pct == null ? "neutral" : pct > 0 ? "pos" : pct < 0 ? "neg" : "neutral";
  return (
    <tr className="border-b border-line/60 font-mono last:border-b-0">
      <td className="px-2 py-1.5 font-sans font-medium">{label}</td>
      <td className="px-2 py-1.5 tabular-nums">{num(move.last)}</td>
      <td className="px-2 py-1.5">
        {pct == null ? (
          <span className="text-ink-faint">—</span>
        ) : (
          <Pill tone={tone}>
            {pct > 0 ? "+" : ""}
            {pct.toFixed(2)}%
          </Pill>
        )}
      </td>
      <td className="px-2 py-1.5 tabular-nums text-ink-muted">
        {num(move.low)}–{num(move.high)}
      </td>
      <td className="px-2 py-1.5 tabular-nums text-ink-muted">
        {move.volume == null ? "—" : move.volume.toLocaleString()}
      </td>
    </tr>
  );
}

export default function ExtendedHoursPanel({
  symbol,
  interval,
}: {
  symbol: string;
  interval: string;
}) {
  const classify = useMemo(
    () =>
      classifyWithAbsences<ExtendedHours>(
        [404],
        "No stored bars for this symbol and interval.",
      ),
    [],
  );
  const { data, unavailable, error } = useKeyedFetch<ExtendedHours>({
    key: `eh:${symbol}|${interval}`,
    url:
      `/api/market-data/${encodeURIComponent(symbol)}/extended-hours?` +
      new URLSearchParams({ bar_interval: interval }),
    classify,
  });

  const reference =
    data?.pre_market?.reference_label ??
    data?.regular?.reference_label ??
    data?.after_hours?.reference_label ??
    null;

  return (
    <Panel
      title="Extended hours"
      description={data ? `session ${data.session_date} · ${data.bar_interval}` : undefined}
    >
      {error ? <Alert>{error}</Alert> : null}
      {unavailable && !data ? <EmptyNote>{unavailable}</EmptyNote> : null}
      {data ? (
        <div className="flex flex-col gap-2">
          <table className="w-full text-left text-[11px]">
            <thead>
              <tr className="border-b border-line uppercase tracking-[0.08em] text-ink-faint">
                <th className="px-2 py-1 font-semibold">phase</th>
                <th className="px-2 py-1 font-semibold">last</th>
                <th className="px-2 py-1 font-semibold">change</th>
                <th className="px-2 py-1 font-semibold">range</th>
                <th className="px-2 py-1 font-semibold">volume</th>
              </tr>
            </thead>
            <tbody>
              {LABELS.map(([key, label]) => (
                <Row key={key} label={label} move={data[key] as PhaseMove | null} />
              ))}
            </tbody>
          </table>
          {reference ? (
            <p className="text-[11px] leading-relaxed text-ink-faint">
              Pre-market and regular are measured against the {reference}; after hours against
              this session&rsquo;s regular close.
            </p>
          ) : null}
          <p className="text-[11px] leading-relaxed text-ink-faint">{data.note}</p>
        </div>
      ) : null}
    </Panel>
  );
}
