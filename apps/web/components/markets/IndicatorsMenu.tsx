"use client";

import { useState } from "react";
import { DEFAULT_INDICATORS, type IndicatorSettings } from "@/lib/indicators";
import { btnGhost, btnSecondary, inputClass } from "@/components/ui/primitives";

/**
 * The Indicators tab beside the interval buttons (Phase 86, D103).
 *
 * A single popover holding the whole vocabulary, each entry a checkbox
 * plus its own parameters. Two entries — session levels and setup signals —
 * are things the chart already drew unconditionally; they live here now
 * rather than as separate checkboxes scattered under the chart, so there is
 * one place that answers "what is on this chart and why".
 *
 * The count on the button is deliberate: with the panel closed, the only
 * thing between the user and a mystery line on the chart is knowing how
 * many things are switched on.
 */

function countOn(s: IndicatorSettings): number {
  return [
    s.sma.on,
    s.ema.on,
    s.bollinger.on,
    s.rsi.on,
    s.macd.on,
    s.vwap.on,
    s.volume.on,
    s.atr.on,
    s.pivot.on,
    s.sessionLevels.on,
    s.signals.on,
  ].filter(Boolean).length;
}

function Row({
  label,
  on,
  onToggle,
  hint,
  children,
}: {
  label: string;
  on: boolean;
  onToggle: (next: boolean) => void;
  hint?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1 border-b border-line/60 py-1.5 last:border-b-0">
      <label className="flex cursor-pointer items-center gap-2 text-xs">
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => onToggle(e.target.checked)}
          aria-label={label}
        />
        <span className="font-medium">{label}</span>
      </label>
      {on && children ? <div className="flex flex-wrap items-center gap-2 pl-6">{children}</div> : null}
      {hint ? <p className="pl-6 text-[11px] leading-snug text-ink-faint">{hint}</p> : null}
    </div>
  );
}

function Num({
  label,
  value,
  onChange,
  step = 1,
  min = 1,
}: {
  label: string;
  value: number;
  onChange: (n: number) => void;
  step?: number;
  min?: number;
}) {
  return (
    <label className="flex items-center gap-1 text-[11px] text-ink-faint">
      {label}
      <input
        type="number"
        className={`${inputClass} w-16 px-1 py-0.5 text-[11px]`}
        value={value}
        min={min}
        step={step}
        aria-label={label}
        onChange={(e) => {
          const n = Number(e.target.value);
          // A blank or non-numeric box must not silently become 0 and
          // produce a "period must be at least 1" series; the last good
          // value simply stays.
          if (Number.isFinite(n) && n >= min) onChange(n);
        }}
      />
    </label>
  );
}

export default function IndicatorsMenu({
  value,
  onChange,
}: {
  value: IndicatorSettings;
  onChange: (next: IndicatorSettings) => void;
}) {
  const [open, setOpen] = useState(false);
  const set = <K extends keyof IndicatorSettings>(
    key: K,
    patch: Partial<IndicatorSettings[K]>,
  ) => onChange({ ...value, [key]: { ...value[key], ...patch } });

  const on = countOn(value);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls="indicators-panel"
        className={open ? btnSecondary : btnGhost}
        data-testid="indicators-tab"
      >
        Indicators{on > 0 ? ` (${on})` : ""}
      </button>

      {open ? (
        <div
          id="indicators-panel"
          data-testid="indicators-panel"
          className="absolute right-0 z-20 mt-1 max-h-[70vh] w-72 overflow-y-auto rounded-md border border-line bg-surface p-3 shadow-xl"
        >
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-xs font-semibold uppercase tracking-[0.08em] text-ink-faint">
              Indicators
            </h3>
            <button
              type="button"
              className="text-[11px] underline underline-offset-2 text-ink-faint hover:text-ink"
              onClick={() => onChange(DEFAULT_INDICATORS)}
            >
              Reset
            </button>
          </div>

          <Row
            label="Bollinger Bands"
            on={value.bollinger.on}
            onToggle={(b) => set("bollinger", { on: b })}
          >
            <Num
              label="period"
              value={value.bollinger.period}
              onChange={(n) => set("bollinger", { period: n })}
            />
            <Num
              label="σ"
              value={value.bollinger.mult}
              step={0.5}
              onChange={(n) => set("bollinger", { mult: n })}
            />
          </Row>

          <Row label="SMA" on={value.sma.on} onToggle={(b) => set("sma", { on: b })}>
            <Num
              label="fast"
              value={value.sma.periods[0]}
              onChange={(n) => set("sma", { periods: [n, value.sma.periods[1]] })}
            />
            <Num
              label="slow"
              value={value.sma.periods[1]}
              onChange={(n) => set("sma", { periods: [value.sma.periods[0], n] })}
            />
          </Row>

          <Row label="EMA" on={value.ema.on} onToggle={(b) => set("ema", { on: b })}>
            <Num
              label="fast"
              value={value.ema.periods[0]}
              onChange={(n) => set("ema", { periods: [n, value.ema.periods[1]] })}
            />
            <Num
              label="slow"
              value={value.ema.periods[1]}
              onChange={(n) => set("ema", { periods: [value.ema.periods[0], n] })}
            />
          </Row>

          <Row
            label="RSI"
            on={value.rsi.on}
            onToggle={(b) => set("rsi", { on: b })}
            hint={value.rsi.on ? "Own pane, with the 70/30 lines." : undefined}
          >
            <Num label="period" value={value.rsi.period} onChange={(n) => set("rsi", { period: n })} />
          </Row>

          <Row label="MACD" on={value.macd.on} onToggle={(b) => set("macd", { on: b })}>
            <Num label="fast" value={value.macd.fast} onChange={(n) => set("macd", { fast: n })} />
            <Num label="slow" value={value.macd.slow} onChange={(n) => set("macd", { slow: n })} />
            <Num
              label="signal"
              value={value.macd.signal}
              onChange={(n) => set("macd", { signal: n })}
            />
          </Row>

          <Row
            label="VWAP"
            on={value.vwap.on}
            onToggle={(b) => set("vwap", { on: b })}
            hint={
              value.vwap.on
                ? "Anchored to each UTC session and reset at the boundary. Needs volume on every bar — if any bar has none the chart says so instead of drawing a partial VWAP."
                : undefined
            }
          />

          <Row label="Volume" on={value.volume.on} onToggle={(b) => set("volume", { on: b })} />

          <Row
            label="ATR"
            on={value.atr.on}
            onToggle={(b) => set("atr", { on: b })}
            hint={value.atr.on ? "Wilder's, matching the ATR the bot sizes stops from." : undefined}
          >
            <Num label="period" value={value.atr.period} onChange={(n) => set("atr", { period: n })} />
          </Row>

          <Row
            label="Pivots"
            on={value.pivot.on}
            onToggle={(b) => set("pivot", { on: b })}
            hint={
              value.pivot.on
                ? "Classic floor pivots from the PREVIOUS session's range, never the one still forming."
                : undefined
            }
          />

          <Row
            label="Session levels"
            on={value.sessionLevels.on}
            onToggle={(b) => set("sessionLevels", { on: b })}
            hint="Prior close/high/low, pre-market range, opening range, VWAP band — computed by the backend."
          />

          <Row
            label="Setup signals (B/S)"
            on={value.signals.on}
            onToggle={(b) => set("signals", { on: b })}
            hint="The scored markers and the score table above the chart."
          />
        </div>
      ) : null}
    </div>
  );
}
