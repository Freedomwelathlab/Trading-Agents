"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type SeriesMarker,
  type SeriesType,
  type UTCTimestamp,
} from "lightweight-charts";
import { EmptyNote } from "@/components/ui/primitives";
import {
  atr as calcAtr,
  bollinger as calcBollinger,
  ema as calcEma,
  macd as calcMacd,
  pivots as calcPivots,
  rsi as calcRsi,
  sma as calcSma,
  volumeBars as calcVolume,
  vwap as calcVwap,
  type Candle,
  type IndicatorSettings,
} from "@/lib/indicators";

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
 * platform's signals marked on them (Phase 70, D088; indicators and a
 * stable viewport added in Phase 86, D103).
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
 * saw when it decided to buy. Every indicator here is computed from those
 * same bars for the same reason — an indicator taken from a vendor's own
 * endpoint would be drawn from a different feed than the candles under it.
 *
 * **A bar with no high/low is skipped, never completed from its close.**
 * Those columns are nullable — a vendor may return a close-only record —
 * and inventing a body and wicks from one number would draw market
 * structure that never existed. The skipped count is surfaced to the caller
 * rather than hidden, so a chart with gaps says so.
 *
 * **The chart object outlives the data (Phase 86).** It is created once per
 * mount and afterwards only fed: `setData` on the existing series, markers
 * and price lines replaced in place. It used to be torn down and rebuilt
 * whenever any prop changed identity — including the `[]` defaults, which
 * are new arrays on every render — so a poll, or any parent re-render,
 * destroyed the canvas and `fitContent()` threw away whatever the user had
 * zoomed into. That is the "it resets a second after I zoom in" bug.
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
    accent: token("--accent", "#7c3aed"),
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
 * The same drawable bars, carrying volume, for the indicator layer.
 *
 * Separate from `toCandles` because the candlestick series does not take a
 * volume and an indicator cannot do without one: `volume: null` stays null
 * all the way through, so a volume-dependent indicator can refuse rather
 * than treat "not reported" as zero.
 */
export function toIndicatorCandles(bars: PriceBar[]): Candle[] {
  const out: Candle[] = [];
  for (const bar of bars) {
    const time = Math.floor(new Date(bar.ts).getTime() / 1000);
    if (!Number.isFinite(time) || bar.open == null || bar.high == null || bar.low == null)
      continue;
    out.push({
      time,
      open: Number(bar.open),
      high: Number(bar.high),
      low: Number(bar.low),
      close: Number(bar.close),
      volume: bar.volume,
    });
  }
  return out;
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

/**
 * How long the chart leaves a hand-set viewport alone before snapping back
 * to the full window (Phase 86).
 *
 * The brief asked for a minute, and a minute is also about the length of a
 * real look: long enough to read a cluster of bars and hover a few signals,
 * short enough that a chart left alone returns to a view that shows
 * everything rather than staying stuck where it was nudged an hour ago.
 */
export const VIEW_HOLD_MS = 60_000;

export function PriceChart({
  bars,
  signals = [],
  scoredSignals = [],
  priceLines = [],
  indicators,
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
  /**
   * Which indicators to draw (Phase 86, D103). Omitted entirely on the
   * backtest detail page, which wants the bare chart it has always had.
   */
  indicators?: IndicatorSettings;
  height?: number;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const mainRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lineRefs = useRef<IPriceLine[]>([]);
  /** Pivot lines are kept apart from the session-level lines so that a
   *  refreshed level set does not silently erase them, and vice versa. */
  const pivotRefs = useRef<IPriceLine[]>([]);
  const overlayRefs = useRef<ISeriesApi<SeriesType>[]>([]);
  const colorsRef = useRef<ReturnType<typeof readThemeColors> | null>(null);
  const [hovered, setHovered] = useState<{ x: number; y: number; items: ScoredSignal[] } | null>(
    null,
  );
  /**
   * Epoch ms until which the user's own viewport is left alone. A ref, not
   * state, because the data effect reads it while deciding whether to
   * re-fit and must not itself re-run when it changes.
   */
  const holdUntilRef = useRef(0);
  const [holdUntil, setHoldUntil] = useState(0);
  const [now, setNow] = useState(0);

  const { candles, skipped } = useMemo(() => toCandles(bars), [bars]);
  const indicatorCandles = useMemo(() => toIndicatorCandles(bars), [bars]);

  /** Content keys: the `[]` defaults are new identities every render, so a
   *  raw prop in a dep array re-runs its effect forever. */
  const linesKey = useMemo(() => JSON.stringify(priceLines), [priceLines]);
  const signalsKey = useMemo(() => JSON.stringify(signals), [signals]);
  const scoredKey = useMemo(() => JSON.stringify(scoredSignals), [scoredSignals]);
  const candlesKey = useMemo(
    () => `${candles.length}:${candles[0]?.time ?? 0}:${candles[candles.length - 1]?.time ?? 0}:${candles[candles.length - 1]?.close ?? 0}`,
    [candles],
  );
  const indicatorsKey = useMemo(() => JSON.stringify(indicators ?? null), [indicators]);

  const fitNow = useCallback(() => {
    holdUntilRef.current = 0;
    setHoldUntil(0);
    chartRef.current?.timeScale().fitContent();
  }, []);

  /** The user touched the viewport: hold it, and start the clock back. */
  const holdView = useCallback(() => {
    const until = Date.now() + VIEW_HOLD_MS;
    holdUntilRef.current = until;
    setHoldUntil(until);
  }, []);

  // --- the chart itself: created once per mount ---------------------------
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const colors = readThemeColors(container);
    colorsRef.current = colors;
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
    mainRef.current = chart.addSeries(CandlestickSeries, {
      upColor: colors.up,
      downColor: colors.down,
      borderUpColor: colors.up,
      borderDownColor: colors.down,
      wickUpColor: colors.up,
      wickDownColor: colors.down,
    });

    // Any deliberate gesture on the canvas counts as "I am reading this" —
    // wheel zoom, drag-pan, pinch. Listening on the container rather than
    // subscribing to visible-range changes matters: a range change also
    // fires when WE re-fit, which would make the chart hold a viewport it
    // set itself and never come back.
    const hold = () => {
      const until = Date.now() + VIEW_HOLD_MS;
      holdUntilRef.current = until;
      setHoldUntil(until);
    };
    container.addEventListener("wheel", hold, { passive: true });
    container.addEventListener("pointerdown", hold);
    container.addEventListener("touchstart", hold, { passive: true });

    return () => {
      container.removeEventListener("wheel", hold);
      container.removeEventListener("pointerdown", hold);
      container.removeEventListener("touchstart", hold);
      chart.remove();
      chartRef.current = null;
      mainRef.current = null;
      lineRefs.current = [];
      pivotRefs.current = [];
      overlayRefs.current = [];
    };
  }, [height]);

  // --- candles ------------------------------------------------------------
  useEffect(() => {
    const series = mainRef.current;
    if (!series || candles.length === 0) return;
    series.setData(candles);
    // Re-fit ONLY if the user is not currently holding a view. `setData`
    // itself preserves the visible logical range, so a poll that adds a bar
    // no longer yanks the viewport either way.
    if (Date.now() >= holdUntilRef.current) chartRef.current?.timeScale().fitContent();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candlesKey]);

  // --- the hold clock: ticks only while a view is actually held -----------
  useEffect(() => {
    if (holdUntil === 0) return;
    setNow(Date.now());
    const id = setInterval(() => {
      const t = Date.now();
      setNow(t);
      if (t >= holdUntilRef.current) {
        holdUntilRef.current = 0;
        setHoldUntil(0);
        chartRef.current?.timeScale().fitContent();
      }
    }, 1000);
    return () => clearInterval(id);
  }, [holdUntil]);

  // --- horizontal levels --------------------------------------------------
  useEffect(() => {
    const series = mainRef.current;
    const colors = colorsRef.current;
    if (!series || !colors) return;
    for (const line of lineRefs.current) series.removePriceLine(line);
    lineRefs.current = [];
    for (const line of priceLines) {
      const price = Number(line.price);
      // A level that is not a finite number is not drawn. Charting `NaN`
      // silently produces a line at an arbitrary position rather than an
      // error, which is the worst of both outcomes.
      if (!Number.isFinite(price)) continue;
      lineRefs.current.push(
        series.createPriceLine({
          price,
          color: colors.levels[line.tone],
          lineWidth: 1,
          lineStyle: line.tone === "vwap" ? 0 : 2,
          axisLabelVisible: true,
          title: line.label,
        }),
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [linesKey, candlesKey]);

  // --- markers and the hover box -----------------------------------------
  useEffect(() => {
    const series = mainRef.current;
    const chart = chartRef.current;
    if (!series || !chart || candles.length === 0) return;

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
        text:
          group.length > 1
            ? `${isBuy ? "B" : "S"}×${group.length}`
            : `${isBuy ? "B" : "S"} ${best.score}`,
      });
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    // Always call it, including with an empty list: turning markers OFF has
    // to clear the ones already drawn, and skipping the call when the list
    // is empty would leave them on screen.
    createSeriesMarkers(series, markers);

    const onMove = (param: Parameters<Parameters<IChartApi["subscribeCrosshairMove"]>[0]>[0]) => {
      const t = param.time as number | undefined;
      const group = t === undefined ? undefined : byTime.get(t);
      if (!group || !param.point) {
        setHovered(null);
        return;
      }
      setHovered({ x: param.point.x, y: param.point.y, items: group });
    };
    chart.subscribeCrosshairMove(onMove);
    return () => {
      chart.unsubscribeCrosshairMove(onMove);
      setHovered(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signalsKey, scoredKey, candlesKey]);

  // --- indicators ---------------------------------------------------------
  const [indicatorNotes, setIndicatorNotes] = useState<string[]>([]);
  useEffect(() => {
    const chart = chartRef.current;
    const series = mainRef.current;
    const colors = colorsRef.current;
    if (!chart || !series || !colors) return;

    for (const s of overlayRefs.current) chart.removeSeries(s);
    overlayRefs.current = [];
    for (const l of pivotRefs.current) series.removePriceLine(l);
    pivotRefs.current = [];
    // Panes are removed back-to-front: removing pane 1 renumbers pane 2.
    for (let i = chart.panes().length - 1; i >= 1; i--) chart.removePane(i);

    if (!indicators || indicatorCandles.length === 0) {
      setIndicatorNotes([]);
      return;
    }

    const notes: string[] = [];
    const overlay = (points: { time: number; value: number }[], color: string, title: string,
                     width: 1 | 2 = 1, dashed = false) => {
      const s = chart.addSeries(LineSeries, {
        color,
        lineWidth: width,
        lineStyle: dashed ? 2 : 0,
        priceLineVisible: false,
        lastValueVisible: false,
        title,
      });
      s.setData(points.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
      overlayRefs.current.push(s);
    };
    const note = (name: string, why?: string) => {
      if (why) notes.push(`${name}: ${why}`);
    };

    const PALETTE = ["#2563eb", "#ea580c", "#0891b2", "#9333ea"];

    if (indicators.sma.on) {
      indicators.sma.periods.forEach((p, i) => {
        const r = calcSma(indicatorCandles, p);
        if (r.unavailable) note(`SMA(${p})`, r.unavailable);
        else overlay(r.points, PALETTE[i % PALETTE.length], `SMA ${p}`);
      });
    }
    if (indicators.ema.on) {
      indicators.ema.periods.forEach((p, i) => {
        const r = calcEma(indicatorCandles, p);
        if (r.unavailable) note(`EMA(${p})`, r.unavailable);
        else overlay(r.points, PALETTE[(i + 2) % PALETTE.length], `EMA ${p}`, 1, true);
      });
    }
    if (indicators.bollinger.on) {
      const { middle, upper, lower } = calcBollinger(
        indicatorCandles,
        indicators.bollinger.period,
        indicators.bollinger.mult,
      );
      if (middle.unavailable) note("Bollinger", middle.unavailable);
      else {
        overlay(upper.points, colors.levels.band, `BB +${indicators.bollinger.mult}σ`);
        overlay(middle.points, colors.inkFaint, `BB ${indicators.bollinger.period}`, 1, true);
        overlay(lower.points, colors.levels.band, `BB -${indicators.bollinger.mult}σ`);
      }
    }
    if (indicators.vwap.on) {
      const r = calcVwap(indicatorCandles);
      if (r.unavailable) note("VWAP", r.unavailable);
      else overlay(r.points, colors.levels.vwap, "VWAP", 2);
    }
    if (indicators.pivot.on) {
      const { levels, unavailable } = calcPivots(indicatorCandles);
      if (unavailable || !levels) note("Pivots", unavailable ?? "No previous session.");
      else {
        const rows: [string, number, string][] = [
          ["R3", levels.r3, colors.levels.band],
          ["R2", levels.r2, colors.levels.band],
          ["R1", levels.r1, colors.down],
          ["P", levels.p, colors.levels.opening],
          ["S1", levels.s1, colors.up],
          ["S2", levels.s2, colors.levels.band],
          ["S3", levels.s3, colors.levels.band],
        ];
        for (const [title, price, color] of rows) {
          pivotRefs.current.push(
            series.createPriceLine({
              price,
              color,
              lineWidth: 1,
              lineStyle: 3,
              axisLabelVisible: true,
              title,
            }),
          );
        }
        notes.push(`Pivots computed from the ${levels.basedOn} session.`);
      }
    }

    // Panes below the price. Each is its own scale, which is the whole
    // point: an RSI plotted on the price axis is a flat line at the bottom
    // of the chart.
    let pane = 0;
    const nextPane = () => {
      pane += 1;
      chart.addPane();
      return pane;
    };
    if (indicators.volume.on) {
      const { bars: vb, unavailable } = calcVolume(indicatorCandles);
      if (vb.length === 0) note("Volume", unavailable);
      else {
        if (unavailable) note("Volume", unavailable);
        const idx = nextPane();
        const s = chart.addSeries(
          HistogramSeries,
          { priceFormat: { type: "volume" }, priceLineVisible: false, title: "Vol" },
          idx,
        );
        s.setData(
          vb.map((b) => ({
            time: b.time as UTCTimestamp,
            value: b.value,
            color: b.up ? colors.up : colors.down,
          })),
        );
        overlayRefs.current.push(s);
        chart.panes()[idx]?.setStretchFactor(0.25);
      }
    }
    if (indicators.rsi.on) {
      const r = calcRsi(indicatorCandles, indicators.rsi.period);
      if (r.unavailable) note(`RSI(${indicators.rsi.period})`, r.unavailable);
      else {
        const idx = nextPane();
        const s = chart.addSeries(
          LineSeries,
          { color: colors.accent, lineWidth: 1, priceLineVisible: false, title: "RSI" },
          idx,
        );
        s.setData(r.points.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
        for (const level of [70, 30])
          s.createPriceLine({
            price: level,
            color: colors.inkFaint,
            lineWidth: 1,
            lineStyle: 2,
            axisLabelVisible: true,
            title: String(level),
          });
        overlayRefs.current.push(s);
        chart.panes()[idx]?.setStretchFactor(0.3);
      }
    }
    if (indicators.macd.on) {
      const m = calcMacd(
        indicatorCandles,
        indicators.macd.fast,
        indicators.macd.slow,
        indicators.macd.signal,
      );
      if (m.macd.unavailable) note("MACD", m.macd.unavailable);
      else {
        const idx = nextPane();
        const hist = chart.addSeries(
          HistogramSeries,
          { priceLineVisible: false, title: "MACD hist" },
          idx,
        );
        hist.setData(
          m.histogram.points.map((p) => ({
            time: p.time as UTCTimestamp,
            value: p.value,
            color: p.value >= 0 ? colors.up : colors.down,
          })),
        );
        const line = chart.addSeries(
          LineSeries,
          { color: "#2563eb", lineWidth: 1, priceLineVisible: false, title: "MACD" },
          idx,
        );
        line.setData(m.macd.points.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
        overlayRefs.current.push(hist, line);
        if (m.signal.unavailable) note("MACD signal", m.signal.unavailable);
        else {
          const sig = chart.addSeries(
            LineSeries,
            { color: "#ea580c", lineWidth: 1, priceLineVisible: false, title: "signal" },
            idx,
          );
          sig.setData(m.signal.points.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
          overlayRefs.current.push(sig);
        }
        chart.panes()[idx]?.setStretchFactor(0.3);
      }
    }
    if (indicators.atr.on) {
      const r = calcAtr(indicatorCandles, indicators.atr.period);
      if (r.unavailable) note(`ATR(${indicators.atr.period})`, r.unavailable);
      else {
        const idx = nextPane();
        const s = chart.addSeries(
          LineSeries,
          { color: "#b45309", lineWidth: 1, priceLineVisible: false, title: "ATR" },
          idx,
        );
        s.setData(r.points.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
        overlayRefs.current.push(s);
        chart.panes()[idx]?.setStretchFactor(0.25);
      }
    }

    setIndicatorNotes(notes);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [indicatorsKey, candlesKey]);

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

  const secondsLeft = holdUntil > 0 ? Math.max(0, Math.ceil((holdUntil - now) / 1000)) : 0;

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

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-faint">
        {secondsLeft > 0 ? (
          <span data-testid="chart-view-hold">
            Your view is held for {secondsLeft}s, then it fits the window again.
          </span>
        ) : (
          <span data-testid="chart-view-auto">
            Auto-fit. Scroll or drag the chart to hold your own view for {VIEW_HOLD_MS / 1000}s.
          </span>
        )}
        <button
          type="button"
          onClick={fitNow}
          onPointerDown={(e) => e.stopPropagation()}
          className="underline underline-offset-2 hover:text-ink"
        >
          Reset view
        </button>
        <button
          type="button"
          onClick={holdView}
          className="underline underline-offset-2 hover:text-ink"
        >
          Hold view
        </button>
      </div>

      {indicatorNotes.length > 0 && (
        <ul className="text-[11px] leading-relaxed text-ink-faint" data-testid="indicator-notes">
          {indicatorNotes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}

      {skipped > 0 && (
        <p className="text-xs" style={{ color: "var(--ink-faint)" }}>
          {skipped} of {bars.length} bar(s) omitted: close-only records with no open/high/low.
        </p>
      )}
    </div>
  );
}
