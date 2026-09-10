import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { StrategyLeaderboard } from "@/components/StrategyLeaderboard";
import { sessionNavigation } from "@/lib/session";
import type { LeaderboardItem } from "@/components/StrategyLeaderboard";

/**
 * Same conventions as `StrategyList.test.tsx` / `BacktestRunList.test.tsx`:
 * `fetch` is stubbed, every assertion is about what the component rendered
 * from a real backend shape, and every error case asserts the backend's
 * own `detail` text is shown verbatim.
 */

function mockJson(status: number, body: unknown, ok = status < 400) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok,
    status,
    json: async () => body,
  });
}

function fourComponentItem(overrides: Partial<LeaderboardItem> = {}): LeaderboardItem {
  return {
    strategy_id: "s-1",
    strategy_name: "SMA crossover v2",
    strategy_version_id: "v-1",
    strategy_version_number: 3,
    score: {
      strategy_version_id: "v-1",
      components: [
        {
          name: "return",
          points: "18.50",
          max_points: "25",
          detail: "total_return_pct=14.80% (scale: 0%→0pts, 20%+→25pts)",
        },
        {
          name: "risk",
          points: "20.00",
          max_points: "25",
          detail: "max_drawdown_pct=5.00% (scale: 0%→25pts, 30%+→0pts)",
        },
        {
          name: "consistency",
          points: "20.83",
          max_points: "25",
          detail: "5/6 profitable windows (83%)",
        },
        {
          name: "parameter_stability",
          points: "15.00",
          max_points: "25",
          detail: "max_return_deviation_pct=6.67 vs 20 scale",
        },
      ],
      total_points: "74.33",
      max_possible_points: "100",
      percentage: "74.33",
      components_measured: 4,
      status: "validated",
      status_reason: "2 of 2 available checks passed",
      latest_backtest_run_id: "r-1",
      latest_walk_forward_run_id: "r-2",
      latest_monte_carlo_run_id: null,
      latest_robustness_run_id: "r-3",
      ...overrides.score,
    },
    ...overrides,
  } as LeaderboardItem;
}

function twoComponentItem(overrides: Partial<LeaderboardItem> = {}): LeaderboardItem {
  return {
    strategy_id: "s-2",
    strategy_name: "RSI mean reversion",
    strategy_version_id: "v-2",
    strategy_version_number: 1,
    score: {
      strategy_version_id: "v-2",
      components: [
        {
          name: "return",
          points: "10.00",
          max_points: "25",
          detail: "total_return_pct=8.00%",
        },
        {
          name: "risk",
          points: "12.00",
          max_points: "25",
          detail: "max_drawdown_pct=12.00%",
        },
      ],
      total_points: "22.00",
      max_possible_points: "50",
      percentage: "44.00",
      components_measured: 2,
      status: "promising",
      status_reason: "Only a backtest has been run so far",
      latest_backtest_run_id: "r-4",
      latest_walk_forward_run_id: null,
      latest_monte_carlo_run_id: null,
      latest_robustness_run_id: null,
      ...overrides.score,
    },
    ...overrides,
  } as LeaderboardItem;
}

function overfitItem(): LeaderboardItem {
  const item = twoComponentItem();
  return {
    ...item,
    strategy_id: "s-3",
    strategy_name: "Overfit special",
    strategy_version_id: "v-3",
    score: { ...item.score, strategy_version_id: "v-3", status: "overfit_risk", percentage: "30.00" },
  };
}

function insufficientItem(): LeaderboardItem {
  const item = twoComponentItem();
  return {
    ...item,
    strategy_id: "s-4",
    strategy_name: "Brand new strat",
    strategy_version_id: "v-4",
    score: {
      ...item.score,
      strategy_version_id: "v-4",
      status: "insufficient_data",
      components: [],
      components_measured: 0,
      percentage: "0.00",
    },
  };
}

describe("StrategyLeaderboard", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders a ranked list in the backend's order with the right status pill per row", async () => {
    const validated = fourComponentItem();
    const promising = twoComponentItem();
    const overfit = overfitItem();
    const insufficient = insufficientItem();
    mockJson(200, {
      items: [validated, promising, overfit, insufficient],
      limit: 50,
      offset: 0,
    });

    render(<StrategyLeaderboard />);

    const rows = await screen.findAllByTestId("leaderboard-row");
    expect(rows).toHaveLength(4);

    // Rank is the 1-indexed array position, never re-sorted client-side.
    expect(within(rows[0]).getByText("1")).toBeInTheDocument();
    expect(within(rows[0]).getByText("SMA crossover v2")).toBeInTheDocument();
    expect(within(rows[0]).getByText("validated")).toBeInTheDocument();

    expect(within(rows[1]).getByText("2")).toBeInTheDocument();
    expect(within(rows[1]).getByText("RSI mean reversion")).toBeInTheDocument();
    expect(within(rows[1]).getByText("promising")).toBeInTheDocument();

    expect(within(rows[2]).getByText("3")).toBeInTheDocument();
    expect(within(rows[2]).getByText("overfit_risk")).toBeInTheDocument();

    expect(within(rows[3]).getByText("4")).toBeInTheDocument();
    expect(within(rows[3]).getByText("insufficient_data")).toBeInTheDocument();

    const link = within(rows[0]).getByRole("link", { name: "SMA crossover v2" });
    expect(link).toHaveAttribute("href", "/strategies/s-1");
  });

  it("renders components_measured as a fraction for both 2- and 4-measured entries", async () => {
    const validated = fourComponentItem();
    const promising = twoComponentItem();
    mockJson(200, { items: [validated, promising], limit: 50, offset: 0 });

    render(<StrategyLeaderboard />);

    const rows = await screen.findAllByTestId("leaderboard-row");
    expect(within(rows[0]).getByText("4/4")).toBeInTheDocument();
    expect(within(rows[1]).getByText("2/4")).toBeInTheDocument();
  });

  it("expands a row to reveal its component detail strings", async () => {
    const validated = fourComponentItem();
    mockJson(200, { items: [validated], limit: 50, offset: 0 });

    render(<StrategyLeaderboard />);

    await screen.findAllByTestId("leaderboard-row");
    expect(
      screen.queryByText(/total_return_pct=14.80%/),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /show breakdown/i }));

    expect(screen.getByText(/total_return_pct=14\.80%/)).toBeInTheDocument();
    expect(screen.getByText(/max_drawdown_pct=5\.00%/)).toBeInTheDocument();
    expect(screen.getByText(/5\/6 profitable windows/)).toBeInTheDocument();
    expect(screen.getByText(/max_return_deviation_pct=6\.67/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /hide breakdown/i }));
    expect(screen.queryByText(/total_return_pct=14\.80%/)).not.toBeInTheDocument();
  });

  it("re-fetches with min_status when the filter changes, and omits it for 'all'", async () => {
    mockJson(200, { items: [], limit: 50, offset: 0 });
    render(<StrategyLeaderboard />);

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith("/api/strategies/leaderboard", {
        cache: "no-store",
      }),
    );

    mockJson(200, { items: [], limit: 50, offset: 0 });
    fireEvent.change(screen.getByLabelText(/minimum status/i), {
      target: { value: "validated" },
    });

    await waitFor(() =>
      expect(fetch).toHaveBeenLastCalledWith(
        "/api/strategies/leaderboard?min_status=validated",
        { cache: "no-store" },
      ),
    );

    mockJson(200, { items: [], limit: 50, offset: 0 });
    fireEvent.change(screen.getByLabelText(/minimum status/i), {
      target: { value: "all" },
    });

    await waitFor(() =>
      expect(fetch).toHaveBeenLastCalledWith("/api/strategies/leaderboard", {
        cache: "no-store",
      }),
    );
  });

  it("shows the filter-specific empty message when min_status is active", async () => {
    mockJson(200, { items: [], limit: 50, offset: 0 });
    render(<StrategyLeaderboard />);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));

    mockJson(200, { items: [], limit: 50, offset: 0 });
    fireEvent.change(screen.getByLabelText(/minimum status/i), {
      target: { value: "validated" },
    });

    await waitFor(() =>
      expect(screen.getByText(/No strategies currently meet this bar/i)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows the no-backtest-yet empty message when min_status is 'all'", async () => {
    mockJson(200, { items: [], limit: 50, offset: 0 });
    render(<StrategyLeaderboard />);

    await waitFor(() =>
      expect(
        screen.getByText(/No strategy has a completed backtest yet/i),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders the backend's own detail on a failure", async () => {
    mockJson(500, { detail: "boom" }, false);
    render(<StrategyLeaderboard />);

    await waitFor(() => expect(screen.getByText(/HTTP 500: boom/)).toBeInTheDocument());
  });

  it("redirects to login on a 401 rather than rendering a raw error", async () => {
    const toLogin = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    mockJson(401, { detail: "Not authenticated" }, false);
    render(<StrategyLeaderboard />);

    await waitFor(() => expect(toLogin).toHaveBeenCalledWith("session-expired"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
