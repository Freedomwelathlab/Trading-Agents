import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import {
  BacktestRunComparison,
  buildComparisonSeries,
  type BacktestRunDetail,
} from "@/components/BacktestRunComparison";
import { sessionNavigation } from "@/lib/session";

function detail(overrides: Partial<BacktestRunDetail> = {}): BacktestRunDetail {
  return {
    id: "run-1",
    strategy_version_id: "v-1",
    symbol: "AAPL.US",
    bar_interval: "1d",
    start_date: "2026-01-01",
    end_date: "2026-01-03",
    starting_cash: "100000",
    status: "succeeded",
    final_equity: "101000",
    total_return_pct: "1.00",
    max_drawdown_pct: "0.50",
    win_rate_pct: "100.00",
    num_trades: 1,
    error_detail: null,
    created_at: "2026-09-01T00:00:00Z",
    completed_at: "2026-09-01T00:01:00Z",
    equity_curve: [
      { date: "2026-01-01", equity: "100000" },
      { date: "2026-01-02", equity: "100500" },
      { date: "2026-01-03", equity: "101000" },
    ],
    trades: [],
    ...overrides,
  };
}

function mockJson(status: number, body: unknown, ok = status < 400) {
  return { ok, status, json: async () => body };
}

/** Minimal ResizeObserver so Recharts' `ResponsiveContainer` actually
 * reads a real size in jsdom (it silently skips sizing entirely when
 * `typeof ResizeObserver === "undefined"`, which jsdom never defines). */
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

describe("BacktestRunComparison", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    vi.stubGlobal("ResizeObserver", ResizeObserverMock);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      width: 600,
      height: 300,
      top: 0,
      left: 0,
      right: 600,
      bottom: 300,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders a hint instead of a broken chart when fewer than 2 runs are selected", () => {
    render(
      <BacktestRunComparison strategyId="s-1" versionId="v-1" selectedRunIds={["run-1"]} />,
    );
    expect(screen.getByText(/select at least two runs/i)).toBeInTheDocument();
    expect(screen.queryByTestId("comparison-chart")).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("renders nothing broken for zero selected runs either", () => {
    render(<BacktestRunComparison strategyId="s-1" versionId="v-1" selectedRunIds={[]} />);
    expect(screen.getByText(/select at least two runs/i)).toBeInTheDocument();
  });

  it("fetches the full detail for every selected run and renders the metrics table", async () => {
    const runA = detail({ id: "run-a", symbol: "AAPL.US" });
    const runB = detail({
      id: "run-b",
      symbol: "MSFT.US",
      total_return_pct: "-2.50",
      final_equity: "97500",
    });
    (fetch as unknown as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(mockJson(200, runA))
      .mockResolvedValueOnce(mockJson(200, runB));

    render(
      <BacktestRunComparison
        strategyId="s-1"
        versionId="v-1"
        selectedRunIds={["run-a", "run-b"]}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("cmp-return-run-a")).toBeInTheDocument());
    expect(screen.getByTestId("cmp-return-run-a")).toHaveTextContent("1.00");
    expect(screen.getByTestId("cmp-return-run-b")).toHaveTextContent("-2.50");
    expect(screen.getByTestId("cmp-equity-run-a")).toHaveTextContent("101000");
    expect(screen.getByTestId("cmp-equity-run-b")).toHaveTextContent("97500");
    expect(screen.getByTestId("cmp-trades-run-a")).toHaveTextContent("1");

    expect(fetch).toHaveBeenNthCalledWith(1, "/api/backtest-runs/run-a", { cache: "no-store" });
    expect(fetch).toHaveBeenNthCalledWith(2, "/api/backtest-runs/run-b", { cache: "no-store" });
  });

  it("shows a failed run's error in the table, excludes it from the chart, and plots only the succeeded ones", async () => {
    const ok1 = detail({ id: "run-ok-1", symbol: "AAPL.US" });
    const ok2 = detail({ id: "run-ok-2", symbol: "MSFT.US" });
    const bad = detail({
      id: "run-bad",
      symbol: "TSLA.US",
      status: "failed",
      final_equity: null,
      total_return_pct: null,
      max_drawdown_pct: null,
      win_rate_pct: null,
      num_trades: null,
      error_detail: "DATA_UNAVAILABLE: vendor returned no closes.",
      equity_curve: [],
    });
    (fetch as unknown as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(mockJson(200, ok1))
      .mockResolvedValueOnce(mockJson(200, ok2))
      .mockResolvedValueOnce(mockJson(200, bad));

    const { container } = render(
      <BacktestRunComparison
        strategyId="s-1"
        versionId="v-1"
        selectedRunIds={["run-ok-1", "run-ok-2", "run-bad"]}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("cmp-status-run-bad")).toBeInTheDocument());

    // The failed run is in the table with its real error, not a fabricated
    // number.
    expect(screen.getByTestId("cmp-status-run-bad")).toHaveTextContent(
      "DATA_UNAVAILABLE: vendor returned no closes.",
    );
    expect(screen.getByTestId("cmp-return-run-bad")).toHaveTextContent("—");
    expect(screen.getByTestId("cmp-chart-status-run-bad")).toHaveTextContent(
      "excluded (run failed)",
    );
    expect(screen.getByTestId("cmp-chart-status-run-ok-1")).toHaveTextContent("plotted");

    // Exactly one rendered line per SUCCEEDED run — the failed run
    // contributes no series to the chart.
    await waitFor(() => {
      expect(container.querySelectorAll(".recharts-line-curve")).toHaveLength(2);
    });
  });

  it("renders every run's line when all selected runs succeeded", async () => {
    const a = detail({ id: "run-a" });
    const b = detail({ id: "run-b" });
    const c = detail({ id: "run-c" });
    (fetch as unknown as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(mockJson(200, a))
      .mockResolvedValueOnce(mockJson(200, b))
      .mockResolvedValueOnce(mockJson(200, c));

    const { container } = render(
      <BacktestRunComparison
        strategyId="s-1"
        versionId="v-1"
        selectedRunIds={["run-a", "run-b", "run-c"]}
      />,
    );

    await waitFor(() => {
      expect(container.querySelectorAll(".recharts-line-curve")).toHaveLength(3);
    });
  });

  it("shows an EmptyNote instead of a chart when every selected run failed", async () => {
    const bad1 = detail({ id: "run-bad-1", status: "failed", equity_curve: [] });
    const bad2 = detail({ id: "run-bad-2", status: "failed", equity_curve: [] });
    (fetch as unknown as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(mockJson(200, bad1))
      .mockResolvedValueOnce(mockJson(200, bad2));

    render(
      <BacktestRunComparison
        strategyId="s-1"
        versionId="v-1"
        selectedRunIds={["run-bad-1", "run-bad-2"]}
      />,
    );

    await waitFor(() =>
      expect(screen.getByText(/none of the selected runs succeeded/i)).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("comparison-chart")).not.toBeInTheDocument();
  });

  it("redirects to login on a 401 from any of the parallel detail fetches", async () => {
    const spy = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    (fetch as unknown as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(mockJson(200, detail({ id: "run-a" })))
      .mockResolvedValueOnce(mockJson(401, { detail: "Not authenticated" }, false));

    render(
      <BacktestRunComparison
        strategyId="s-1"
        versionId="v-1"
        selectedRunIds={["run-a", "run-b"]}
      />,
    );

    await waitFor(() => expect(spy).toHaveBeenCalledWith("session-expired"));
    expect(screen.queryByTestId("comparison-chart")).not.toBeInTheDocument();
  });

  it("renders a real backend error for a non-401 failure on any run", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(mockJson(200, detail({ id: "run-a" })))
      .mockResolvedValueOnce(mockJson(404, { detail: "No backtest run with id run-b." }, false));

    render(
      <BacktestRunComparison
        strategyId="s-1"
        versionId="v-1"
        selectedRunIds={["run-a", "run-b"]}
      />,
    );

    await waitFor(() =>
      expect(screen.getByText(/No backtest run with id run-b\./)).toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toHaveTextContent("HTTP 404");
  });

  it("shows a real error state on network failure", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("down"));

    render(
      <BacktestRunComparison
        strategyId="s-1"
        versionId="v-1"
        selectedRunIds={["run-a", "run-b"]}
      />,
    );

    await waitFor(() =>
      expect(
        screen.getByText(/DATA_UNAVAILABLE: could not reach the trading API/),
      ).toBeInTheDocument(),
    );
  });
});

describe("buildComparisonSeries", () => {
  it("merges succeeded runs' curves into one dataset keyed by date", () => {
    const a = detail({
      id: "run-a",
      equity_curve: [
        { date: "2026-01-01", equity: "100" },
        { date: "2026-01-02", equity: "110" },
      ],
    });
    const b = detail({
      id: "run-b",
      equity_curve: [
        { date: "2026-01-01", equity: "200" },
        { date: "2026-01-02", equity: "190" },
      ],
    });
    const series = buildComparisonSeries([a, b]);
    expect(series).toEqual([
      { date: "2026-01-01", "run-a": 100, "run-b": 200 },
      { date: "2026-01-02", "run-a": 110, "run-b": 190 },
    ]);
  });

  it("excludes a failed run's (empty) curve entirely, contributing no key", () => {
    const ok = detail({
      id: "run-ok",
      equity_curve: [{ date: "2026-01-01", equity: "100" }],
    });
    const bad = detail({ id: "run-bad", status: "failed", equity_curve: [] });
    const series = buildComparisonSeries([ok, bad]);
    expect(series).toEqual([{ date: "2026-01-01", "run-ok": 100 }]);
    expect(series[0]).not.toHaveProperty("run-bad");
  });

  it("leaves a run's key absent on a date only another run has, rather than inventing a value", () => {
    const a = detail({
      id: "run-a",
      equity_curve: [
        { date: "2026-01-01", equity: "100" },
        { date: "2026-01-02", equity: "110" },
      ],
    });
    const b = detail({
      id: "run-b",
      equity_curve: [{ date: "2026-01-01", equity: "200" }],
    });
    const series = buildComparisonSeries([a, b]);
    const day2 = series.find((row) => row.date === "2026-01-02");
    expect(day2).toBeDefined();
    expect(day2).not.toHaveProperty("run-b");
  });
});
