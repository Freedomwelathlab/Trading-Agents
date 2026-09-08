import { describe, it, expect } from "vitest";
import { computeDrawdownSeries, computeMonthlyReturns } from "@/lib/backtestMetrics";

describe("computeDrawdownSeries", () => {
  it("returns an empty array for an empty curve", () => {
    expect(computeDrawdownSeries([])).toEqual([]);
  });

  it("returns all zeros for a monotonically increasing curve", () => {
    const out = computeDrawdownSeries([
      { date: "2026-01-01", equity: 100 },
      { date: "2026-01-02", equity: 110 },
      { date: "2026-01-03", equity: 125 },
    ]);
    expect(out.map((p) => p.drawdownPct)).toEqual([0, 0, 0]);
  });

  it("tracks the running peak, not the global maximum of the whole curve", () => {
    // Peak reaches 100 at index 0, dips to 80 (20% down from 100 - not
    // from the later 110 high), then a new high at 110 resets the peak,
    // and the final dip to 90 is 18.1818...% down from that new peak.
    const out = computeDrawdownSeries([
      { date: "d1", equity: 100 },
      { date: "d2", equity: 80 },
      { date: "d3", equity: 95 },
      { date: "d4", equity: 110 },
      { date: "d5", equity: 90 },
    ]);
    expect(out[0].drawdownPct).toBeCloseTo(0, 6);
    // 20% down from peak=100, NOT from the later global max of 110.
    expect(out[1].drawdownPct).toBeCloseTo(20, 6);
    expect(out[2].drawdownPct).toBeCloseTo(5, 6);
    expect(out[3].drawdownPct).toBeCloseTo(0, 6);
    expect(out[4].drawdownPct).toBeCloseTo(((110 - 90) / 110) * 100, 6);
  });

  it("preserves the date on every point", () => {
    const out = computeDrawdownSeries([{ date: "2026-03-04", equity: 500 }]);
    expect(out).toEqual([{ date: "2026-03-04", drawdownPct: 0 }]);
  });
});

describe("computeMonthlyReturns", () => {
  it("returns an empty array for an empty curve", () => {
    expect(computeMonthlyReturns([])).toEqual([]);
  });

  it("computes percent change from each month's first to last plotted point", () => {
    const out = computeMonthlyReturns([
      { date: "2026-01-02", equity: 100 },
      { date: "2026-01-15", equity: 100 },
      { date: "2026-01-30", equity: 110 },
    ]);
    expect(out).toEqual([{ year: 2026, month: 1, returnPct: 10 }]);
  });

  it("returns 0 for a month with exactly one point in the curve", () => {
    const out = computeMonthlyReturns([
      { date: "2026-02-01", equity: 100 },
      { date: "2026-03-01", equity: 90 },
      { date: "2026-03-31", equity: 99 },
    ]);
    const feb = out.find((m) => m.month === 2);
    expect(feb).toEqual({ year: 2026, month: 2, returnPct: 0 });
  });

  it("handles a Dec -> Jan year boundary as two distinct, correctly-ordered months", () => {
    const out = computeMonthlyReturns([
      { date: "2025-12-01", equity: 100 },
      { date: "2025-12-31", equity: 90 },
      { date: "2026-01-02", equity: 90 },
      { date: "2026-01-31", equity: 108 },
    ]);
    expect(out).toEqual([
      { year: 2025, month: 12, returnPct: -10 },
      { year: 2026, month: 1, returnPct: 20 },
    ]);
  });

  it("sorts output chronologically regardless of input order", () => {
    const out = computeMonthlyReturns([
      { date: "2026-03-01", equity: 100 },
      { date: "2026-01-01", equity: 100 },
      { date: "2026-02-01", equity: 100 },
    ]);
    expect(out.map((m) => `${m.year}-${m.month}`)).toEqual(["2026-1", "2026-2", "2026-3"]);
  });
});
