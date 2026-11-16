import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import BacktestReport, { EquityChart } from "@/components/library/BacktestReport";
import type { BacktestResult, Metrics } from "@/components/library/types";
import { num, pct } from "@/components/library/types";

/**
 * The library report (Phase 108, D135) shows the backend's verdict and
 * numbers as sent: strategy beside buy-and-hold, missing figures as a
 * dash - never a zero - and every data warning the API attached.
 */

const M = (over: Partial<Metrics> = {}): Metrics => ({
  start: "2007-02-01",
  end: "2026-11-13",
  days: 4980,
  years: 19.78,
  total_return: 3.75,
  cagr: 0.082,
  volatility: 0.119,
  sharpe: 0.69,
  sortino: 0.91,
  max_drawdown: -0.276,
  calmar: 0.3,
  t_stat: 3.06,
  best_year: 0.26,
  worst_year: -0.12,
  positive_years: 15,
  total_years: 20,
  ...over,
});

const RESULT: BacktestResult = {
  strategy_id: "trend_200d",
  symbols: ["SPY.US"],
  benchmark_symbols: ["SPY.US"],
  params: { sma_days: 200 },
  lag: 1,
  published_year: 2007,
  segments: [
    { label: "Whole period", strategy: M({ cagr: 0.096 }), benchmark: M({ cagr: 0.107, max_drawdown: -0.552 }) },
    { label: "After publication (from 2007)", strategy: M(), benchmark: null },
  ],
  verdict: {
    code: "lower_risk",
    headline: "Lower return than buy-and-hold, with less risk",
    detail: "After publication: +8.2% a year, Sharpe 0.69, worst drawdown -27.6%.",
  },
  curve: [
    { d: "2007-02-01", s: 1, b: 1 },
    { d: "2016-01-04", s: 2.1, b: 1.9 },
    { d: "2026-11-13", s: 4.75, b: 7.4 },
  ],
  years: [
    { year: 2025, strategy: 0.11, benchmark: 0.17 },
    { year: 2026, strategy: -0.02, benchmark: null },
  ],
  trades: 31,
  turnover_per_year: 1.6,
  cost_drag_per_year: 0.0005,
  avg_exposure: 0.74,
  target: [{ symbol: "SPY.US", weight: 1, note: "close 754.4 is above its 200-day average 690.1" }],
  decided_on: "2026-10-30",
  data: [
    {
      symbol: "SPY.US", vendor_symbol: "SPY", currency: "USD", first: "1993-01-29", last: "2026-11-13",
      bars: 8507, last_close: 754.37, cost_bps: 3, removed: [], truncated_before: null,
    },
    {
      symbol: "TCS.IN", vendor_symbol: "TCS.NS", currency: "INR", first: "2004-05-06", last: "2026-11-13",
      bars: 5500, last_close: 3100, cost_bps: 15, removed: ["2005-07-28"], truncated_before: "2004-05-06",
    },
  ],
  warnings: ["Cash earns nothing in this backtest. Prices are dividend- and split-adjusted."],
  source: "yahoo",
  as_of: "2026-11-13",
  generated_at: "2026-11-16T08:00:00Z",
};

describe("BacktestReport", () => {
  it("leads with the verdict and shows strategy beside buy-and-hold", () => {
    render(<BacktestReport result={RESULT} />);
    expect(screen.getByTestId("library-verdict").textContent).toBe("lower risk");
    expect(screen.getByText(RESULT.verdict.headline)).toBeTruthy();
    expect(screen.getByText("+9.6%")).toBeTruthy();
    expect(screen.getByText("+10.7%")).toBeTruthy();
    expect(screen.getByText("-55.2%")).toBeTruthy();
    expect(screen.getByText(/31 rebalances/)).toBeTruthy();
  });

  it("shows a dash, not a zero, where there is no benchmark", () => {
    render(<BacktestReport result={RESULT} />);
    // The second segment has no benchmark: nine measure rows, each a dash.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(9);
  });

  it("shows what the rule holds, the warnings, and what the screen removed", () => {
    render(<BacktestReport result={RESULT} />);
    expect(screen.getByTestId("library-holdings").textContent).toContain("above its 200-day average");
    expect(screen.getByTestId("library-warnings").textContent).toContain("Cash earns nothing");
    expect(screen.getByText(/1 bad tick\(s\) dropped; history before 2004-05-06 discarded/)).toBeTruthy();
    expect(screen.getByText("clean")).toBeTruthy();
  });

  it("says the rule is in cash when it holds nothing", () => {
    render(<BacktestReport result={{ ...RESULT, target: [] }} />);
    expect(screen.getByText(/entirely in cash/)).toBeTruthy();
  });

  it("draws both curves and skips the chart with too little data", () => {
    const { container, rerender } = render(<EquityChart curve={RESULT.curve} />);
    expect(container.querySelectorAll("polyline").length).toBe(2);
    rerender(<EquityChart curve={RESULT.curve.slice(0, 1)} />);
    expect(container.querySelector("svg")).toBeNull();
  });

  it("formats missing numbers as a dash", () => {
    expect(pct(null)).toBe("—");
    expect(num(undefined)).toBe("—");
    expect(pct(0.1234)).toBe("+12.3%");
    expect(pct(-0.05, 0)).toBe("-5%");
  });
});
