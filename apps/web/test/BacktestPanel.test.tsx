import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import BacktestPanel, {
  buildBacktestPoints,
  formatDetail,
  todayUtc,
  utcDaysAgo,
} from "@/components/BacktestPanel";
import { sessionNavigation } from "@/lib/session";

/**
 * The success-path fixtures below are the response bodies documented in
 * docs/API.md's `POST /backtests` section and hand-derived in D035's own
 * unit tests — not invented numbers. They stand in for a live run only
 * because no market-data vendor credentials are available in this
 * environment (D025/D035 recorded the same limitation); the
 * NOT_CONFIGURED path is the one verified against the real backend.
 */
function okResult(overrides: Record<string, unknown> = {}) {
  return {
    symbol: "AAPL.US",
    start_date: "2026-08-24",
    end_date: "2026-08-28",
    starting_cash: "100000",
    final_equity: "99966.830",
    total_return_pct: "-0.0331700",
    num_trades: 1,
    win_rate_pct: "0",
    max_drawdown_pct: "0.1714300",
    equity_curve: [
      { date: "2026-08-24", equity: "100000" },
      { date: "2026-08-25", equity: "100000" },
      { date: "2026-08-26", equity: "99863.600" },
      { date: "2026-08-27", equity: "99863.600" },
      { date: "2026-08-28", equity: "99966.830" },
    ],
    portfolio_modified_trades: 0,
    portfolio_modify_risk_blocked_trades: 0,
    portfolio_rejected_trades: 0,
    ...overrides,
  };
}

function mockJson(status: number, body: unknown, ok = status < 400) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok,
    status,
    json: async () => body,
  });
}

function run() {
  fireEvent.click(screen.getByRole("button", { name: /run backtest/i }));
}

describe("BacktestPanel", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("posts the real request shape the backend documents", async () => {
    mockJson(200, okResult());

    render(<BacktestPanel />);
    fireEvent.change(screen.getByLabelText(/^symbol$/i), { target: { value: "MSFT.US" } });
    fireEvent.change(screen.getByLabelText(/starting cash/i), { target: { value: "50000" } });
    run();

    await waitFor(() =>
      expect(screen.getByTestId("backtest-equity-line")).toBeInTheDocument(),
    );
    const call = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[0]).toBe("/api/backtests");
    expect(call[1].method).toBe("POST");
    const sent = JSON.parse(call[1].body);
    expect(Object.keys(sent).sort()).toEqual([
      "end_date",
      "start_date",
      "starting_cash",
      "symbol",
    ]);
    expect(sent.symbol).toBe("MSFT.US");
    expect(sent.starting_cash).toBe("50000");
    // The form defaults end_date to today UTC, which is the only value
    // the backend accepts (D025).
    expect(sent.end_date).toBe(todayUtc());
  });

  it("renders the real equity curve and headline metrics", async () => {
    mockJson(200, okResult());

    render(<BacktestPanel />);
    run();

    await waitFor(() =>
      expect(screen.getByTestId("backtest-equity-line")).toBeInTheDocument(),
    );
    // One plotted vertex per real trading day — nothing interpolated.
    expect(
      screen.getByTestId("backtest-equity-line").getAttribute("points")?.split(" "),
    ).toHaveLength(5);
    expect(screen.getByTestId("bt-final-equity")).toHaveTextContent("99966.830");
    expect(screen.getByTestId("bt-total-return")).toHaveTextContent("-0.0331700");
    expect(screen.getByTestId("bt-num-trades")).toHaveTextContent("1");
    expect(screen.getByTestId("bt-win-rate")).toHaveTextContent("0");
    expect(screen.getByTestId("bt-max-drawdown")).toHaveTextContent("0.1714300");
    // The tabulated points are the same values, verbatim.
    expect(screen.getAllByText("99863.600").length).toBeGreaterThanOrEqual(1);
  });

  it("renders the D035 Portfolio Manager counters when it intervened", async () => {
    mockJson(
      200,
      okResult({
        portfolio_modified_trades: 3,
        portfolio_modify_risk_blocked_trades: 1,
        portfolio_rejected_trades: 2,
      }),
    );

    render(<BacktestPanel />);
    run();

    await waitFor(() => expect(screen.getByTestId("bt-pm-modified")).toHaveTextContent("3"));
    expect(screen.getByTestId("bt-pm-modify-blocked")).toHaveTextContent("1");
    expect(screen.getByTestId("bt-pm-rejected")).toHaveTextContent("2");
    // The three are never collapsed into one number.
    expect(screen.getByText(/portfolio_modify_risk_blocked_trades/)).toBeInTheDocument();
    expect(
      screen.getByText(/Risk Engine rejections are a\s+separate gate/),
    ).toBeInTheDocument();
  });

  it("still shows all three counters at zero, with the caveat that zero is not approval", async () => {
    mockJson(200, okResult());

    render(<BacktestPanel />);
    run();

    await waitFor(() => expect(screen.getByTestId("bt-pm-modified")).toHaveTextContent("0"));
    expect(screen.getByTestId("bt-pm-modify-blocked")).toHaveTextContent("0");
    expect(screen.getByTestId("bt-pm-rejected")).toHaveTextContent("0");
    expect(
      screen.getByText(/not, on its own, that every\s+trade was approved/),
    ).toBeInTheDocument();
  });

  it("says win rate is not applicable rather than showing 0% for zero round trips", async () => {
    mockJson(200, okResult({ num_trades: 0, win_rate_pct: "0" }));

    render(<BacktestPanel />);
    run();

    await waitFor(() =>
      expect(screen.getByTestId("bt-win-rate")).toHaveTextContent(
        /n\/a \(no completed round trips\)/,
      ),
    );
  });

  it("draws a flat curve as flat instead of dividing by zero", async () => {
    mockJson(
      200,
      okResult({
        num_trades: 0,
        final_equity: "10000",
        total_return_pct: "0",
        max_drawdown_pct: "0",
        equity_curve: [
          { date: "2026-08-26", equity: "10000" },
          { date: "2026-08-27", equity: "10000" },
          { date: "2026-08-28", equity: "10000" },
        ],
      }),
    );

    render(<BacktestPanel />);
    run();

    await waitFor(() =>
      expect(screen.getByTestId("backtest-equity-line")).toBeInTheDocument(),
    );
    const ys = screen
      .getByTestId("backtest-equity-line")
      .getAttribute("points")!
      .split(" ")
      .map((p) => p.split(",")[1]);
    expect(new Set(ys).size).toBe(1);
    expect(ys.every((y) => Number.isFinite(Number(y)))).toBe(true);
  });

  it("surfaces the real NOT_CONFIGURED sentinel when no history provider is wired", async () => {
    mockJson(400, { detail: "NOT_CONFIGURED: no history provider." }, false);

    render(<BacktestPanel />);
    run();

    await waitFor(() =>
      expect(screen.getByText(/NOT_CONFIGURED: no history provider/)).toBeInTheDocument(),
    );
    // No fabricated curve stands in for the run that could not happen.
    expect(screen.queryByTestId("backtest-equity-line")).not.toBeInTheDocument();
    expect(screen.queryByTestId("bt-portfolio-counters")).not.toBeInTheDocument();
  });

  it("surfaces the real UNSUPPORTED_DATE_RANGE sentinel", async () => {
    mockJson(
      400,
      { detail: "UNSUPPORTED_DATE_RANGE: end_date must be today (UTC)." },
      false,
    );

    render(<BacktestPanel />);
    fireEvent.change(screen.getByLabelText(/end date/i), { target: { value: "2020-01-02" } });
    run();

    await waitFor(() =>
      expect(screen.getByText(/UNSUPPORTED_DATE_RANGE/)).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("backtest-equity-line")).not.toBeInTheDocument();
  });

  it("surfaces a 400 DATA_UNAVAILABLE for too little history", async () => {
    mockJson(
      400,
      { detail: "DATA_UNAVAILABLE: vendor returned 12 closes, need 25." },
      false,
    );

    render(<BacktestPanel />);
    run();

    await waitFor(() =>
      expect(
        screen.getByText(/DATA_UNAVAILABLE: vendor returned 12 closes, need 25/),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText(/HTTP 400/)).toBeInTheDocument();
  });

  it("surfaces a 502 DATA_UNAVAILABLE when the vendor itself failed", async () => {
    mockJson(502, { detail: "DATA_UNAVAILABLE: vendor request failed." }, false);

    render(<BacktestPanel />);
    run();

    await waitFor(() =>
      expect(screen.getByText(/DATA_UNAVAILABLE: vendor request failed/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/HTTP 502/)).toBeInTheDocument();
  });

  it("renders a real 422 validation error legibly instead of [object Object]", async () => {
    mockJson(
      422,
      {
        detail: [
          {
            loc: ["body", "starting_cash"],
            msg: "Input should be greater than 0",
            type: "greater_than",
          },
        ],
      },
      false,
    );

    render(<BacktestPanel />);
    fireEvent.change(screen.getByLabelText(/starting cash/i), { target: { value: "0" } });
    run();

    await waitFor(() =>
      expect(
        screen.getByText(/body\.starting_cash: Input should be greater than 0/),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
  });

  it("redirects to login on a 401 instead of showing a dead form", async () => {
    const spy = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    mockJson(401, { detail: "Not authenticated" }, false);

    render(<BacktestPanel />);
    run();

    await waitFor(() => expect(spy).toHaveBeenCalledWith("session-expired"));
    expect(screen.queryByTestId("backtest-equity-line")).not.toBeInTheDocument();
  });

  it("shows a real error state on network failure", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("down"));

    render(<BacktestPanel />);
    run();

    await waitFor(() =>
      expect(
        screen.getByText(/DATA_UNAVAILABLE: could not reach the trading API/),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("backtest-equity-line")).not.toBeInTheDocument();
  });

  it("says so when the run returned an empty curve, rather than charting nothing", async () => {
    mockJson(200, okResult({ equity_curve: [] }));

    render(<BacktestPanel />);
    run();

    await waitFor(() =>
      expect(screen.getByText(/returned an empty equity curve/i)).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("backtest-equity-line")).not.toBeInTheDocument();
  });
});

describe("buildBacktestPoints", () => {
  it("plots exactly one vertex per returned point, in order, spanning the width", () => {
    const { points, min, max } = buildBacktestPoints([
      { date: "2026-08-26", equity: "100" },
      { date: "2026-08-27", equity: "300" },
      { date: "2026-08-28", equity: "200" },
    ]);
    expect(points).toHaveLength(3);
    expect(min).toBe(100);
    expect(max).toBe(300);
    expect(points[0].x).toBeLessThan(points[1].x);
    expect(points[1].x).toBeLessThan(points[2].x);
    // Higher equity = smaller y (SVG origin is top-left).
    expect(points[1].y).toBeLessThan(points[0].y);
    expect(points[1].y).toBeLessThan(points[2].y);
  });

  it("centres a single point instead of collapsing it to an edge", () => {
    const { points } = buildBacktestPoints([{ date: "2026-08-28", equity: "100" }]);
    expect(points).toHaveLength(1);
    expect(points[0].x).toBeGreaterThan(100);
    expect(Number.isFinite(points[0].y)).toBe(true);
  });
});

describe("formatDetail", () => {
  it("passes a plain string sentinel through untouched", () => {
    expect(formatDetail("NOT_CONFIGURED: no history provider.")).toBe(
      "NOT_CONFIGURED: no history provider.",
    );
  });

  it("joins FastAPI's 422 list into readable loc: msg pairs", () => {
    expect(
      formatDetail([
        { loc: ["body", "end_date"], msg: "Input should be a valid date" },
        { loc: ["body", "symbol"], msg: "String should have at least 1 character" },
      ]),
    ).toBe(
      "body.end_date: Input should be a valid date; body.symbol: String should have at least 1 character",
    );
  });

  it("returns null for a missing detail so the caller can fall back to the status", () => {
    expect(formatDetail(undefined)).toBeNull();
    expect(formatDetail(null)).toBeNull();
  });
});

describe("date helpers", () => {
  it("produces YYYY-MM-DD UTC dates with the start strictly before today", () => {
    expect(todayUtc()).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(utcDaysAgo(30)).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(utcDaysAgo(30) < todayUtc()).toBe(true);
  });
});
