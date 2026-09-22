"use client";

import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type SeriesMarker,
  type UTCTimestamp,
} from "lightweight-charts";
import { EmptyNote } from "@/components/ui/primitives";

export type PriceBar = {
  ts: string;
  open: string | null;
  high: string | null;
  low: string | null;
  close: string;
  volume: number | null;
  source: string;
};

export type PriceLine = {
  /** Decimal string, exactly as the backend sent it. */
  price: string;
  label: string;
  /** One of the semantic roles below, not a raw colour — the component
   *  resolves it against the live theme so the line reads in both. */
  tone: "prior" | "premarket" | "opening" | "vwap" | "band";
};

export type SignalMarker = {
  /** The bar this signal landed on, as an ISO date (`YYYY-MM-DD`). */
  date: string;
  side: "buy" | "sell";
  /** Executed price, shown in the marker's label. */
  price: string;
};

/**
 * A TradingView-style candlestick chart of OUR OWN bars, with THIS
 * platform's signals marked on them (Phase 70, D088).
 *
 * **Why `lightweight-charts` and not TradingView's free embed widget.** The
 * widget renders TradingView's own data inside a sealed cross-origin
 * iframe: there is no supported way to hand it a bar series, and no way to
 * draw a marker on it. A chart of someone else's bars cannot honestly carry
 * this system's signals, because the bar a marker sits on would not be the
 * bar the backtest replayed — the two feeds can and do differ on exact
 * OHLC. `lightweight-charts` is the same rendering engine, Apache-2.0
 * licensed, published by TradingView, and it takes the caller's series —
 * which is the entire requirement here.
 *
 * **Every bar comes from `market_data_bars`**, through the same
 * `MarketDataStore` the backtest engine reads. That identity is the point:
 * a BUY marker is only meaningful sitting on the bar the engine actually
 * saw when it decided to buy.
 *
 * **A bar with no high/low is skipped, never completed from its close.**
 * Those columns are nullable — a vendor may return a close-only record —
 * and inventing a body and wicks from one number would draw market
 * structure that never existed. The skipped count is surfaced to the caller
 * rather than hidden, so a chart with gaps says so.
 */

/** Series colours resolved from the app's CSS custom properties. */
function readThemeColors(el: HTMLElement) {
  const style = getComputedStyle(el);
  const token = (name: string, fallback: string) =>
    style.getPropertyValue(name).trim() || fallback;
  return {
    // `--pos`/`--neg` are this dashboard's existing up/down pair, used by
    // every other chart here, so a rising candle is the same green as a
    // rising equity curve rather than a second convention.
    up: token("--pos", "#047857"),
    down: token("--neg", "#b91c1c"),
    ink: token("--ink", "#0f172a"),
    inkFaint: token("--ink-faint", "#64748b"),
    grid: token("--grid", "#e2e8f0"),
    surface: token("--surface", "#ffffff"),
    // Session levels, keyed by ROLE rather than by colour, so a caller
    // names what a line means and this decides how it reads (Phase 74).
    // Each falls back to a literal only if the token is missing, which
    // keeps the chart legible even on a page that never loaded the
    // dashboard's stylesheet.
    levels: {
      prior: token("--ink-faint", "#64748b"),
      premarket: token("--accent", "#7c3aed"),
      opening: token("--level-opening", "#b45309"),
      vwap: token("--level-vwap", "#0369a1"),
      band: token("--grid", "#cbd5e1"),
    },
  };
}

/**
 * Bars the chart can actually draw, plus how many were dropped.
 *
 * `lightweight-charts` wants seconds since the epoch. The backend sends
 * each bar's own timezone-aware vendor timestamp, so this converts rather
 * than assuming a session close — the same discipline `engine_v2` follows
 * in using `bar.ts` instead of an invented close time.
 *
 * Exported so its rules - which bar is drawable, which is skipped - can be
 * asserted directly. `lightweight-charts` renders to a canvas that jsdom
 * does not implement, so a render-level test could not see a candle even if
 * one were drawn; testing the transform is testing the decision.
 */
export function toCandles(bars: PriceBar[]) {
  const candles = [];
  let skipped = 0;
  for (const bar of bars) {
    const time = Math.floor(new Date(bar.ts).getTime() / 1000);
    if (
      !Number.isFinite(time) ||
      bar.open == null ||
      bar.high == null ||
      bar.low == null
    ) {
      skipped += 1;
      continue;
    }
    candles.push({
      time: time as UTCTimestamp,
      open: Number(bar.open),
      high: Number(bar.high),
      low: Number(bar.low),
      close: Number(bar.close),
    });
  }
  return { candles, skipped };
}

/**
 * Signal markers, snapped onto the chart's own time axis.
 *
 * A trade carries a calendar DATE while a bar carries a timestamp, so each
 * marker is matched to the first bar falling on that UTC date. A signal
 * whose bar is not in the loaded window is DROPPED rather than pinned to
 * the nearest one: a marker sitting on a bar the trade did not happen on
 * would be a plausible-looking lie about when the strategy acted, which is
 * worse than a marker that is simply absent.
 *
 * Exported for direct assertion, for the same reason `toCandles` is.
 */
export function toMarkers(
  signals: SignalMarker[],
  candleTimes: UTCTimestamp[],
): SeriesMarker<UTCTimestamp>[] {
  const byDate = new Map<string, UTCTimestamp>();
  for (const time of candleTimes) {
    const key = new Date(time * 1000).toISOString().slice(0, 10);
    if (!byDate.has(key)) byDate.set(key, time);
  }

  const markers: SeriesMarker<UTCTimestamp>[] = [];
  for (const signal of signals) {
    const time = byDate.get(signal.date);
    if (time === undefined) continue;
    const isBuy = signal.side === "buy";
    markers.push({
      time,
      position: isBuy ? "belowBar" : "aboveBar",
      shape: isBuy ? "arrowUp" : "arrowDown",
      color: isBuy ? "#047857" : "#b91c1c",
      text: `${isBuy ? "BUY" : "SELL"} @ ${Number(signal.price).toFixed(2)}`,
    });
  }
  // The library requires markers in ascending time order and silently
  // misrenders otherwise; the trade ledger arrives sorted by entry_date,
  // which interleaves exits out of order once both sides are mapped.
  return markers.sort((a, b) => (a.time as number) - (b.time as number));
}

/**
 * A scored setup signal drawn on the chart (Phase 85, D102). `evidence`
 * is the detector's own reasoning, shown in the hover box — the point of
 * the box is that a marker can be interrogated rather than trusted.
 */
export type ScoredSignal = {
  ts: string;
  setup: string;
  side: "B" | "S";
  price: string;
  stop_price: string;
  score: number;
  evidence: Record<string, string>;
};

export function PriceChart({
  bars,
  signals = [],
  scoredSignals = [],
  priceLines = [],
  height = 360,
}: {
  bars: PriceBar[];
  signals?: SignalMarker[];
  /** Setup signals with scores; drawn as B/S markers with a hover box. */
  scoredSignals?: ScoredSignal[];
  /**
   * Session-anchored levels drawn as horizontal lines (Phase 74, D092).
   *
   * Additive and defaulted to empty, so the backtest detail page that
   * already renders this chart is unchanged. A level the backend reported
   * as null must simply not be in this array — there is no "draw it at
   * zero" path, because a previous-day low at zero sits below every candle
   * and reads as a level price never reached.
   */
  priceLines?: PriceLine[];
  height?: number;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const [hovered, setHovered] = useState<{ x: number; y: number; items: ScoredSignal[] } | null>(
    null,
  );

  const { candles, skipped } = toCandles(bars);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || candles.length === 0) return;

    const colors = readThemeColors(container);
    const chart = createChart(container, {
      height,
      layout: {
        background: { color: colors.surface },
        textColor: colors.inkFaint,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: colors.grid },
        horzLines: { color: colors.grid },
      },
      rightPriceScale: { borderColor: colors.grid },
      timeScale: {
        borderColor: colors.grid,
        // Intraday intervals are the reason this is on: without it a 5m
        // series collapses every bar in a day onto one date label.
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: { vertLine: { color: colors.inkFaint }, horzLine: { color: colors.inkFaint } },
      // The container carries an explicit height (below) and a fluid width,
      // so `autoSize` is what makes the canvas track a window resize or a
      // panel reflow instead of staying at its first-paint width.
      //
      // NOTE for anyone debugging a blank chart in an automated browser:
      // a chart whose canvases are all stuck at the HTML default 300x150
      // is almost certainly in a tab with `document.visibilityState ===
      // "hidden"`, where no ResizeObserver callback fires at all - not
      // this library's, and not a plain one either. That is a property of
      // the hidden tab, not of this component, and passing an explicit
      // `width` here does NOT work around it (measured: the canvases stay
      // at 300x150 either way). Bring the window to the foreground.
      autoSize: true,
    });
    chartRef.current = chart;

    const series = chart.addSeries(CandlestickSeries, {
      upColor: colors.up,
      downColor: colors.down,
      borderUpColor: colors.up,
      borderDownColor: colors.down,
      wickUpColor: colors.up,
      wickDownColor: colors.down,
    });
    series.setData(candles);

    for (const line of priceLines) {
      const price = Number(line.price);
      // A level that is not a finite number is not drawn. Charting `NaN`
      // silently produces a line at an arbitrary position rather than an
      // error, which is the worst of both outcomes.
      if (!Number.isFinite(price)) continue;
      series.createPriceLine({
        price,
        color: colors.levels[line.tone],
        lineWidth: 1,
        lineStyle: line.tone === "vwap" ? 0 : 2,
        axisLabelVisible: true,
        title: line.label,
      });
    }

    const times = candles.map((c) => c.time);
    const markers = toMarkers(signals, times);

    // Phase 85 (D102): one marker per scored signal. Several setups can
    // fire on the same bar, so they are grouped and the marker text shows
    // the count; the hover box lists each one with its own score.
    const byTime = new Map<number, ScoredSignal[]>();
    for (const sig of scoredSignals) {
      const t = Math.floor(new Date(sig.ts).getTime() / 1000);
      if (!Number.isFinite(t)) continue;
      const group = byTime.get(t);
      if (group) group.push(sig);
      else byTime.set(t, [sig]);
    }
    for (const [t, group] of byTime) {
      const buys = group.filter((g) => g.side === "B").length;
      const isBuy = buys >= group.length - buys;
      const best = group.reduce((a, b) => (b.score > a.score ? b : a));
      markers.push({
        time: t as UTCTimestamp,
        position: isBuy ? "belowBar" : "aboveBar",
        shape: isBuy ? "arrowUp" : "arrowDown",
        color: isBuy ? "#047857" : "#b91c1c",
        text: group.length > 1 ? `${isBuy ? "B" : "S"}×${group.length}` : `${isBuy ? "B" : "S"} ${best.score}`,
      });
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    if (markers.length > 0) createSeriesMarkers(series, markers);

    if (byTime.size > 0) {
      chart.subscribeCrosshairMove((param) => {
        const t = param.time as number | undefined;
        const group = t === undefined ? undefined : byTime.get(t);
        if (!group || !param.point) {
          setHovered(null);
          return;
        }
        setHovered({ x: param.point.x, y: param.point.y, items: group });
      });
    }

    chart.timeScale().fitContent();

    return () => {
      chart.remove();
      chartRef.current = null;
    };
    // `candles`/`markers` are derived fresh each render from these two
    // props; depending on the derived arrays instead would rebuild the
    // chart on every render, since they are new identities each time.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bars, signals, scoredSignals, priceLines, height]);

  if (bars.length === 0) {
    return (
      <EmptyNote>
        No bars are ingested for this symbol and interval over this window. Backfill them
        (POST /admin/market-data/backfill) — nothing here is drawn from anything else.
      </EmptyNote>
    );
  }

  if (candles.length === 0) {
    return (
      <EmptyNote>
        All {bars.length} bar(s) in this window are close-only — no open, high or low — so no
        candles can be drawn. A candlestick invented from a close alone would be fabricated
        market structure.
      </EmptyNote>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="relative">
        <div
          ref={containerRef}
          data-testid="price-chart"
          style={{ width: "100%", height }}
        />
        {/* Phase 85 (D102): the score box. Shown only while the crosshair
            is on a bar that actually produced a signal, so it never
            invents a reading for a bar that had none. */}
        {hovered ? (
          <div
            data-testid="signal-score-box"
            className="pointer-events-none absolute z-10 w-64 rounded-md border border-line bg-surface/95 p-2 shadow-lg backdrop-blur"
            style={{
              left: Math.min(hovered.x + 12, 9999),
              top: Math.max(hovered.y - 12, 4),
            }}
          >
            {hovered.items.map((sig, n) => (
              <div key={`${sig.setup}-${n}`} className={n > 0 ? "mt-2 border-t border-line pt-2" : ""}>
                <p className="flex items-center justify-between text-xs font-semibold">
                  <span className={sig.side === "B" ? "text-pos" : "text-neg"}>
                    {sig.side === "B" ? "BUY" : "SELL"} · {sig.setup}
                  </span>
                  <span className="font-mono">score {sig.score}</span>
                </p>
                <p className="mt-0.5 font-mono text-[11px] text-ink-muted">
                  entry {Number(sig.price).toFixed(2)} · stop {Number(sig.stop_price).toFixed(2)}
                </p>
                {Object.entries(sig.evidence).length > 0 ? (
                  <ul className="mt-1 text-[11px] leading-snug text-ink-muted">
                    {Object.entries(sig.evidence).map(([k, v]) => (
                      <li key={k}>
                        <span className="text-ink-faint">{k}:</span> {v}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ))}
          </div>
        ) : null}
      </div>
      {skipped > 0 && (
        <p className="text-xs" style={{ color: "var(--ink-faint)" }}>
          {skipped} of {bars.length} bar(s) omitted: close-only records with no open/high/low.
        </p>
      )}
    </div>
  );
}
