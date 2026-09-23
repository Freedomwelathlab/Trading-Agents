import { describe, expect, it } from "vitest";
import {
  atr,
  bollinger,
  ema,
  macd,
  pivots,
  rsi,
  sma,
  volumeBars,
  vwap,
  type Candle,
} from "@/lib/indicators";

/**
 * Indicator arithmetic (Phase 86, D103).
 *
 * These assert two separate things and it is worth naming which is which.
 * The first is the arithmetic itself, against hand-computed values — an
 * indicator that is subtly wrong still draws a confident line, so the only
 * useful test is one whose expected number was worked out independently.
 * The second is the refusal behaviour: every function here must decline to
 * produce a series it cannot honestly compute, and say why, rather than
 * padding a short window or reading a null volume as zero.
 */

const DAY = 86_400;

function bar(i: number, close: number, over: Partial<Candle> = {}): Candle {
  return {
    time: Date.parse("2026-01-01T00:00:00Z") / 1000 + i * DAY,
    open: close,
    high: close + 1,
    low: close - 1,
    close,
    volume: 100,
    ...over,
  };
}

const closes = (values: number[]) => values.map((v, i) => bar(i, v));

describe("sma", () => {
  it("emits its first point on the bar that completes the window", () => {
    const r = sma(closes([1, 2, 3, 4]), 3);
    expect(r.unavailable).toBeUndefined();
    // (1+2+3)/3 = 2 on bar index 2, (2+3+4)/3 = 3 on bar index 3.
    expect(r.points.map((p) => p.value)).toEqual([2, 3]);
    expect(r.points[0].time).toBe(bar(2, 3).time);
  });

  it("refuses a window shorter than the period instead of averaging what it has", () => {
    const r = sma(closes([1, 2]), 5);
    expect(r.points).toEqual([]);
    expect(r.unavailable).toContain("Needs 5 bars");
  });
});

describe("ema", () => {
  it("seeds from the SMA of the first window, then smooths", () => {
    const r = ema(closes([1, 2, 3, 4]), 3);
    // Seed = (1+2+3)/3 = 2. k = 2/4 = 0.5. Next = 4*0.5 + 2*0.5 = 3.
    expect(r.points.map((p) => p.value)).toEqual([2, 3]);
  });
});

describe("bollinger", () => {
  it("uses the population standard deviation of the window", () => {
    const r = bollinger(closes([2, 4, 6]), 3, 2);
    // mean 4; deviations -2,0,2 -> variance (4+0+4)/3 = 8/3; sd ~1.632993.
    const sd = Math.sqrt(8 / 3);
    expect(r.middle.points[0].value).toBeCloseTo(4, 10);
    expect(r.upper.points[0].value).toBeCloseTo(4 + 2 * sd, 10);
    expect(r.lower.points[0].value).toBeCloseTo(4 - 2 * sd, 10);
  });
});

describe("rsi", () => {
  it("is 100 when every move in the seed window is a gain", () => {
    // No losses at all -> avgLoss 0 -> the function returns 100 rather
    // than dividing by zero.
    const r = rsi(closes([1, 2, 3, 4]), 3);
    expect(r.points[0].value).toBe(100);
  });

  it("matches a hand-computed Wilder value on a mixed window", () => {
    // Deltas: +2, -1, +2. Seed over 3: avgGain 4/3, avgLoss 1/3.
    // RS = 4 -> RSI = 100 - 100/5 = 80.
    const r = rsi(closes([10, 12, 11, 13]), 3);
    expect(r.points[0].value).toBeCloseTo(80, 10);
  });

  it("needs one more bar than its period, because the first bar has no delta", () => {
    expect(rsi(closes([1, 2, 3]), 3).unavailable).toContain("Needs 4 bars");
  });
});

describe("macd", () => {
  it("refuses a fast period that is not shorter than the slow one", () => {
    const r = macd(closes([1, 2, 3, 4, 5]), 26, 12, 9);
    expect(r.macd.unavailable).toContain("shorter");
  });

  it("produces a signal seeded from the MACD line, not from price", () => {
    const r = macd(closes(Array.from({ length: 60 }, (_, i) => 100 + i)), 12, 26, 9);
    expect(r.macd.unavailable).toBeUndefined();
    expect(r.signal.unavailable).toBeUndefined();
    // On a perfectly linear ramp the fast EMA sits above the slow one, so
    // the MACD line is positive throughout.
    expect(r.macd.points.every((p) => p.value > 0)).toBe(true);
    // The histogram is MACD minus signal and exists only where both do.
    expect(r.histogram.points.length).toBe(r.signal.points.length);
  });
});

describe("atr", () => {
  it("computes Wilder's ATR from true ranges, skipping the first bar", () => {
    // Every bar here has high = close+1, low = close-1 and closes are flat,
    // so every true range is exactly 2 and the ATR is 2 throughout.
    const r = atr(closes([5, 5, 5, 5, 5]), 3);
    expect(r.points.every((p) => Math.abs(p.value - 2) < 1e-12)).toBe(true);
    // 5 bars -> 4 true ranges -> seed uses 3, leaving 2 points.
    expect(r.points).toHaveLength(2);
  });
});

describe("vwap", () => {
  it("resets at the session boundary rather than averaging the whole window", () => {
    const day1 = [
      { ...bar(0, 10), volume: 1 },
      { ...bar(0, 20), volume: 1, time: bar(0, 20).time + 3600 },
    ];
    const day2 = [{ ...bar(1, 100), volume: 1 }];
    const r = vwap([...day1, ...day2]);
    expect(r.unavailable).toBeUndefined();
    // Typical price is (h+l+c)/3, and with h=c+1, l=c-1 that is just c.
    expect(r.points[0].value).toBeCloseTo(10, 10);
    expect(r.points[1].value).toBeCloseTo(15, 10);
    // A cumulative VWAP would be (10+20+100)/3 ≈ 43.3 here; the reset is
    // what makes the third point the new session's own number.
    expect(r.points[2].value).toBeCloseTo(100, 10);
  });

  it("refuses the whole series when any bar has no volume", () => {
    const r = vwap([bar(0, 10), { ...bar(1, 11), volume: null }]);
    expect(r.points).toEqual([]);
    expect(r.unavailable).toContain("no volume");
  });
});

describe("volumeBars", () => {
  it("reports how many bars were omitted rather than dropping them silently", () => {
    const r = volumeBars([bar(0, 10), { ...bar(1, 11), volume: null }]);
    expect(r.bars).toHaveLength(1);
    expect(r.unavailable).toContain("1 bar(s) omitted");
  });
});

describe("pivots", () => {
  it("computes from the previous session, never the one still forming", () => {
    // Session 1: high 11, low 9, close 10. Session 2 is the current one.
    const r = pivots([bar(0, 10), bar(1, 50)]);
    expect(r.levels).not.toBeNull();
    expect(r.levels!.basedOn).toBe("2026-01-01");
    expect(r.levels!.p).toBeCloseTo((11 + 9 + 10) / 3, 10);
    expect(r.levels!.r1).toBeCloseTo(2 * 10 - 9, 10);
    expect(r.levels!.s1).toBeCloseTo(2 * 10 - 11, 10);
  });

  it("declines when the window holds only one session", () => {
    const r = pivots([bar(0, 10)]);
    expect(r.levels).toBeNull();
    expect(r.unavailable).toContain("previous session");
  });
});
