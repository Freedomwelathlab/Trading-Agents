/**
 * Chart indicators (Phase 86, D103).
 *
 * Every function here is pure, deterministic, and computed from the bars
 * this platform actually stored — the same `market_data_bars` rows the
 * backtest engine replays. Nothing is fetched from a vendor's own
 * indicator endpoint, because an indicator drawn from one feed sitting on
 * candles from another is a picture of two different markets.
 *
 * Three rules hold throughout:
 *
 * 1. **A window that is not full produces no point.** An SMA(20) has no
 *    value on bar 19; emitting the mean of the bars so far would draw a
 *    line that looks like an average of 20 and is not. Each series
 *    therefore starts late, and the chart simply has no line there.
 * 2. **A missing input is never substituted.** A bar with `volume: null`
 *    is a record the vendor did not give us, not a bar with zero volume.
 *    Volume-dependent indicators report `unavailable` rather than draw a
 *    flat zero line or silently skip the bar and misalign the rest.
 * 3. **Nothing is smoothed across a gap.** These operate on the bar
 *    sequence as stored; they do not interpolate missing sessions.
 */

export type Candle = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  /** null when the vendor returned no volume for this bar. */
  volume: number | null;
};

export type Point = { time: number; value: number };

/** A computed series, or the reason it could not be computed. */
export type Computed = { points: Point[]; unavailable?: string };

const ok = (points: Point[]): Computed => ({ points });
const no = (why: string): Computed => ({ points: [], unavailable: why });

/** Simple moving average. First point lands on bar `period - 1`. */
export function sma(candles: Candle[], period: number): Computed {
  if (period < 1) return no("Period must be at least 1.");
  if (candles.length < period)
    return no(`Needs ${period} bars; this window has ${candles.length}.`);
  const out: Point[] = [];
  let sum = 0;
  for (let i = 0; i < candles.length; i++) {
    sum += candles[i].close;
    if (i >= period) sum -= candles[i - period].close;
    if (i >= period - 1) out.push({ time: candles[i].time, value: sum / period });
  }
  return ok(out);
}

/**
 * Exponential moving average, seeded with the SMA of the first `period`
 * bars.
 *
 * Seeding with the first close instead (the other common convention) makes
 * the opening values of the series depend entirely on one bar, which shows
 * up as a visible hook at the left edge that is an artefact of the seed,
 * not of the market.
 */
export function ema(candles: Candle[], period: number): Computed {
  if (period < 1) return no("Period must be at least 1.");
  if (candles.length < period)
    return no(`Needs ${period} bars; this window has ${candles.length}.`);
  const k = 2 / (period + 1);
  let seed = 0;
  for (let i = 0; i < period; i++) seed += candles[i].close;
  let prev = seed / period;
  const out: Point[] = [{ time: candles[period - 1].time, value: prev }];
  for (let i = period; i < candles.length; i++) {
    prev = candles[i].close * k + prev * (1 - k);
    out.push({ time: candles[i].time, value: prev });
  }
  return ok(out);
}

/**
 * Bollinger Bands: an SMA with a population standard deviation band.
 *
 * Population (divide by n), not sample (n-1): the window IS the data being
 * described, not a draw from a wider set, and the two differ enough at
 * period 20 to move the band visibly.
 */
export function bollinger(
  candles: Candle[],
  period: number,
  mult: number,
): { middle: Computed; upper: Computed; lower: Computed } {
  const middle = sma(candles, period);
  if (middle.unavailable)
    return { middle, upper: no(middle.unavailable), lower: no(middle.unavailable) };
  const upper: Point[] = [];
  const lower: Point[] = [];
  for (let n = 0; n < middle.points.length; n++) {
    const end = n + period; // exclusive index into candles
    let acc = 0;
    for (let i = end - period; i < end; i++) {
      const d = candles[i].close - middle.points[n].value;
      acc += d * d;
    }
    const sd = Math.sqrt(acc / period);
    upper.push({ time: middle.points[n].time, value: middle.points[n].value + mult * sd });
    lower.push({ time: middle.points[n].time, value: middle.points[n].value - mult * sd });
  }
  return { middle, upper: ok(upper), lower: ok(lower) };
}

/**
 * Wilder's RSI.
 *
 * Wilder's own smoothing (`(prev * (n-1) + current) / n`), not a simple
 * mean of the last n gains — the two give materially different readings
 * and every overbought/oversold threshold in the literature is quoted
 * against Wilder's.
 */
export function rsi(candles: Candle[], period: number): Computed {
  if (period < 1) return no("Period must be at least 1.");
  if (candles.length < period + 1)
    return no(`Needs ${period + 1} bars; this window has ${candles.length}.`);
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = candles[i].close - candles[i - 1].close;
    if (d >= 0) gain += d;
    else loss -= d;
  }
  let avgGain = gain / period;
  let avgLoss = loss / period;
  const value = (g: number, l: number) => (l === 0 ? 100 : 100 - 100 / (1 + g / l));
  const out: Point[] = [{ time: candles[period].time, value: value(avgGain, avgLoss) }];
  for (let i = period + 1; i < candles.length; i++) {
    const d = candles[i].close - candles[i - 1].close;
    avgGain = (avgGain * (period - 1) + Math.max(d, 0)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(-d, 0)) / period;
    out.push({ time: candles[i].time, value: value(avgGain, avgLoss) });
  }
  return ok(out);
}

/**
 * MACD line, signal line and histogram.
 *
 * The signal is an EMA of the MACD LINE, so it is seeded from the MACD
 * series rather than from price — which is why this composes `ema` over a
 * synthetic candle list instead of calling it on `candles` again.
 */
export function macd(
  candles: Candle[],
  fast: number,
  slow: number,
  signalPeriod: number,
): { macd: Computed; signal: Computed; histogram: Computed } {
  if (fast >= slow) {
    const why = "The fast period must be shorter than the slow one.";
    return { macd: no(why), signal: no(why), histogram: no(why) };
  }
  const f = ema(candles, fast);
  const s = ema(candles, slow);
  if (f.unavailable || s.unavailable) {
    const why = s.unavailable ?? f.unavailable ?? "Not enough bars.";
    return { macd: no(why), signal: no(why), histogram: no(why) };
  }
  const fastAt = new Map(f.points.map((p) => [p.time, p.value]));
  const line: Point[] = [];
  for (const p of s.points) {
    const fv = fastAt.get(p.time);
    if (fv === undefined) continue;
    line.push({ time: p.time, value: fv - p.value });
  }
  const asCandles: Candle[] = line.map((p) => ({
    time: p.time,
    open: p.value,
    high: p.value,
    low: p.value,
    close: p.value,
    volume: null,
  }));
  const sig = ema(asCandles, signalPeriod);
  if (sig.unavailable)
    return { macd: ok(line), signal: sig, histogram: no(sig.unavailable) };
  const signalAt = new Map(sig.points.map((p) => [p.time, p.value]));
  const hist: Point[] = [];
  for (const p of line) {
    const sv = signalAt.get(p.time);
    if (sv === undefined) continue;
    hist.push({ time: p.time, value: p.value - sv });
  }
  return { macd: ok(line), signal: sig, histogram: ok(hist) };
}

/**
 * Average True Range (Wilder), seeded with the simple mean of the first
 * `period` true ranges.
 *
 * True range needs a previous close, so the first bar contributes none —
 * this is the same definition `apps/api/app/backtesting/indicators.py`
 * uses, deliberately, so the ATR a stop is sized from server-side and the
 * ATR drawn here are the same number.
 */
export function atr(candles: Candle[], period: number): Computed {
  if (period < 1) return no("Period must be at least 1.");
  if (candles.length < period + 1)
    return no(`Needs ${period + 1} bars; this window has ${candles.length}.`);
  const tr: number[] = [];
  for (let i = 1; i < candles.length; i++) {
    const c = candles[i];
    const prevClose = candles[i - 1].close;
    tr.push(
      Math.max(c.high - c.low, Math.abs(c.high - prevClose), Math.abs(c.low - prevClose)),
    );
  }
  let acc = 0;
  for (let i = 0; i < period; i++) acc += tr[i];
  let prev = acc / period;
  const out: Point[] = [{ time: candles[period].time, value: prev }];
  for (let i = period; i < tr.length; i++) {
    prev = (prev * (period - 1) + tr[i]) / period;
    out.push({ time: candles[i + 1].time, value: prev });
  }
  return ok(out);
}

/**
 * Session-anchored VWAP.
 *
 * Anchored to the UTC calendar day of each bar and reset at the boundary,
 * because a VWAP that never resets is a cumulative average of the whole
 * loaded window — a number no desk trades off and one that drifts further
 * from the tape the longer the window is.
 *
 * Returns `unavailable` when ANY bar in the window has no volume rather
 * than skipping those bars: a VWAP computed from a subset of a session's
 * prints is not that session's VWAP, and it would look exactly like one.
 */
export function vwap(candles: Candle[]): Computed {
  if (candles.length === 0) return no("No bars.");
  const missing = candles.filter((c) => c.volume == null).length;
  if (missing > 0)
    return no(
      `${missing} of ${candles.length} bar(s) carry no volume, so a session VWAP cannot be computed from this window.`,
    );
  const out: Point[] = [];
  let day = "";
  let pv = 0;
  let vol = 0;
  for (const c of candles) {
    const key = new Date(c.time * 1000).toISOString().slice(0, 10);
    if (key !== day) {
      day = key;
      pv = 0;
      vol = 0;
    }
    const typical = (c.high + c.low + c.close) / 3;
    pv += typical * (c.volume as number);
    vol += c.volume as number;
    if (vol > 0) out.push({ time: c.time, value: pv / vol });
  }
  return out.length > 0 ? ok(out) : no("Every bar in this window reported zero volume.");
}

/** Volume bars, coloured by whether the bar closed up or down. */
export function volumeBars(
  candles: Candle[],
): { bars: { time: number; value: number; up: boolean }[]; unavailable?: string } {
  const withVol = candles.filter((c) => c.volume != null);
  if (withVol.length === 0)
    return { bars: [], unavailable: "No bar in this window carries volume." };
  const bars = withVol.map((c, i) => ({
    time: c.time,
    value: c.volume as number,
    up: c.close >= (withVol[i - 1]?.close ?? c.open),
  }));
  const note =
    withVol.length < candles.length
      ? `${candles.length - withVol.length} bar(s) omitted: no volume reported.`
      : undefined;
  return { bars, unavailable: note };
}

export type PivotLevels = {
  /** The session whose high/low/close these were computed from. */
  basedOn: string;
  p: number;
  r1: number;
  r2: number;
  r3: number;
  s1: number;
  s2: number;
  s3: number;
};

/**
 * Classic floor-trader pivots for the LAST session in the window, computed
 * from the session BEFORE it.
 *
 * Computing them from the current, still-forming session would be the
 * look-ahead error in miniature: the levels a trader uses today are fixed
 * by yesterday's range before today opens. `basedOn` names the session
 * used so the reading can be checked rather than trusted.
 */
export function pivots(candles: Candle[]): { levels: PivotLevels | null; unavailable?: string } {
  const sessions = new Map<string, { high: number; low: number; close: number }>();
  for (const c of candles) {
    const key = new Date(c.time * 1000).toISOString().slice(0, 10);
    const cur = sessions.get(key);
    if (!cur) sessions.set(key, { high: c.high, low: c.low, close: c.close });
    else {
      cur.high = Math.max(cur.high, c.high);
      cur.low = Math.min(cur.low, c.low);
      cur.close = c.close;
    }
  }
  const keys = [...sessions.keys()].sort();
  if (keys.length < 2)
    return {
      levels: null,
      unavailable:
        "Pivots need the previous session's range; this window holds only one session.",
    };
  const key = keys[keys.length - 2];
  const { high, low, close } = sessions.get(key)!;
  const p = (high + low + close) / 3;
  const range = high - low;
  return {
    levels: {
      basedOn: key,
      p,
      r1: 2 * p - low,
      s1: 2 * p - high,
      r2: p + range,
      s2: p - range,
      r3: high + 2 * (p - low),
      s3: low - 2 * (high - p),
    },
  };
}

/**
 * Which indicators the chart draws, and with what parameters.
 *
 * One flat object rather than a list of instances: the menu is a fixed
 * vocabulary (the ten entries the brief named), each either on or off, so
 * a shape that could express "three RSIs" would be modelling a product
 * this does not have.
 */
export type IndicatorSettings = {
  sma: { on: boolean; periods: number[] };
  ema: { on: boolean; periods: number[] };
  bollinger: { on: boolean; period: number; mult: number };
  rsi: { on: boolean; period: number };
  macd: { on: boolean; fast: number; slow: number; signal: number };
  vwap: { on: boolean };
  volume: { on: boolean };
  atr: { on: boolean; period: number };
  pivot: { on: boolean };
  /** The session levels the terminal used to draw unconditionally. */
  sessionLevels: { on: boolean };
  /** The scored B/S setup markers, likewise. */
  signals: { on: boolean };
};

/**
 * The defaults reproduce exactly what the chart drew before this menu
 * existed — session levels and setup markers on, nothing else — so opening
 * the terminal shows the same picture it did yesterday and every new
 * indicator is something the user turned on themselves.
 */
export const DEFAULT_INDICATORS: IndicatorSettings = {
  sma: { on: false, periods: [20, 50] },
  ema: { on: false, periods: [9, 21] },
  bollinger: { on: false, period: 20, mult: 2 },
  rsi: { on: false, period: 14 },
  macd: { on: false, fast: 12, slow: 26, signal: 9 },
  vwap: { on: false },
  volume: { on: false },
  atr: { on: false, period: 14 },
  pivot: { on: false },
  sessionLevels: { on: true },
  signals: { on: true },
};

/** A colour ROLE, resolved against the live theme by the chart. Keeping
 *  the plan free of literal colours is what lets it be computed during
 *  render, before any DOM exists to read theme tokens from. */
export type Tone =
  | "series-a"
  | "series-b"
  | "series-c"
  | "series-d"
  | "faint"
  | "band"
  | "vwap"
  | "accent"
  | "up"
  | "down"
  | "pivot";

export type OverlayPlan = {
  points: Point[];
  tone: Tone;
  title: string;
  width: 1 | 2;
  dashed: boolean;
};

export type PivotLinePlan = { title: string; price: number; tone: Tone };

export type PanePlan =
  | {
      kind: "line";
      series: { points: Point[]; tone: Tone; title: string }[];
      stretch: number;
      /** Horizontal reference lines drawn on the FIRST series of the pane. */
      levels: number[];
    }
  | {
      kind: "histogram";
      bars: { time: number; value: number; up: boolean }[];
      title: string;
      stretch: number;
      volumeFormat: boolean;
      /** Lines drawn in the same pane, e.g. MACD and its signal. */
      series: { points: Point[]; tone: Tone; title: string }[];
    };

export type IndicatorPlan = {
  overlays: OverlayPlan[];
  pivotLines: PivotLinePlan[];
  panes: PanePlan[];
  notes: string[];
};

const OVERLAY_TONES: Tone[] = ["series-a", "series-b", "series-c", "series-d"];

/**
 * Turn settings plus bars into everything the chart must draw — a pure
 * function, so the notes it produces ("VWAP: 3 bars carry no volume") are
 * available during render instead of having to be pushed into state from
 * inside the drawing effect.
 */
export function planIndicators(
  candles: Candle[],
  settings: IndicatorSettings | undefined,
): IndicatorPlan {
  const plan: IndicatorPlan = { overlays: [], pivotLines: [], panes: [], notes: [] };
  if (!settings || candles.length === 0) return plan;

  const note = (name: string, why?: string) => {
    if (why) plan.notes.push(`${name}: ${why}`);
  };
  const overlay = (
    points: Point[],
    tone: Tone,
    title: string,
    width: 1 | 2 = 1,
    dashed = false,
  ) => plan.overlays.push({ points, tone, title, width, dashed });

  if (settings.sma.on) {
    settings.sma.periods.forEach((p, i) => {
      const r = sma(candles, p);
      if (r.unavailable) note(`SMA(${p})`, r.unavailable);
      else overlay(r.points, OVERLAY_TONES[i % OVERLAY_TONES.length], `SMA ${p}`);
    });
  }
  if (settings.ema.on) {
    settings.ema.periods.forEach((p, i) => {
      const r = ema(candles, p);
      if (r.unavailable) note(`EMA(${p})`, r.unavailable);
      else overlay(r.points, OVERLAY_TONES[(i + 2) % OVERLAY_TONES.length], `EMA ${p}`, 1, true);
    });
  }
  if (settings.bollinger.on) {
    const { middle, upper, lower } = bollinger(
      candles,
      settings.bollinger.period,
      settings.bollinger.mult,
    );
    if (middle.unavailable) note("Bollinger", middle.unavailable);
    else {
      overlay(upper.points, "band", `BB +${settings.bollinger.mult}σ`);
      overlay(middle.points, "faint", `BB ${settings.bollinger.period}`, 1, true);
      overlay(lower.points, "band", `BB -${settings.bollinger.mult}σ`);
    }
  }
  if (settings.vwap.on) {
    const r = vwap(candles);
    if (r.unavailable) note("VWAP", r.unavailable);
    else overlay(r.points, "vwap", "VWAP", 2);
  }
  if (settings.pivot.on) {
    const { levels, unavailable } = pivots(candles);
    if (unavailable || !levels) note("Pivots", unavailable ?? "No previous session.");
    else {
      plan.pivotLines = [
        { title: "R3", price: levels.r3, tone: "band" },
        { title: "R2", price: levels.r2, tone: "band" },
        { title: "R1", price: levels.r1, tone: "down" },
        { title: "P", price: levels.p, tone: "pivot" },
        { title: "S1", price: levels.s1, tone: "up" },
        { title: "S2", price: levels.s2, tone: "band" },
        { title: "S3", price: levels.s3, tone: "band" },
      ];
      plan.notes.push(`Pivots computed from the ${levels.basedOn} session.`);
    }
  }

  if (settings.volume.on) {
    const { bars, unavailable } = volumeBars(candles);
    if (bars.length === 0) note("Volume", unavailable);
    else {
      note("Volume", unavailable);
      plan.panes.push({
        kind: "histogram",
        bars,
        title: "Vol",
        stretch: 0.25,
        volumeFormat: true,
        series: [],
      });
    }
  }
  if (settings.rsi.on) {
    const r = rsi(candles, settings.rsi.period);
    if (r.unavailable) note(`RSI(${settings.rsi.period})`, r.unavailable);
    else {
      plan.panes.push({
        kind: "line",
        series: [{ points: r.points, tone: "accent", title: "RSI" }],
        stretch: 0.3,
        levels: [70, 30],
      });
    }
  }
  if (settings.macd.on) {
    const m = macd(candles, settings.macd.fast, settings.macd.slow, settings.macd.signal);
    if (m.macd.unavailable) note("MACD", m.macd.unavailable);
    else {
      const series = [{ points: m.macd.points, tone: "series-a" as Tone, title: "MACD" }];
      if (m.signal.unavailable) note("MACD signal", m.signal.unavailable);
      else series.push({ points: m.signal.points, tone: "series-b", title: "signal" });
      plan.panes.push({
        kind: "histogram",
        bars: m.histogram.points.map((p) => ({
          time: p.time,
          value: p.value,
          up: p.value >= 0,
        })),
        title: "MACD hist",
        stretch: 0.3,
        volumeFormat: false,
        series,
      });
    }
  }
  if (settings.atr.on) {
    const r = atr(candles, settings.atr.period);
    if (r.unavailable) note(`ATR(${settings.atr.period})`, r.unavailable);
    else {
      plan.panes.push({
        kind: "line",
        series: [{ points: r.points, tone: "pivot", title: "ATR" }],
        stretch: 0.25,
        levels: [],
      });
    }
  }

  return plan;
}
