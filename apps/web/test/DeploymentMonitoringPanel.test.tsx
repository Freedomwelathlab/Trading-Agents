import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { DeploymentMonitoringPanel } from "@/components/DeploymentMonitoringPanel";
import type { DeploymentMonitoringResponse } from "@/components/DeploymentMonitoringPanel";

function monitoring(
  overrides: Partial<DeploymentMonitoringResponse> = {},
): DeploymentMonitoringResponse {
  return {
    deployment_id: "dep-1",
    as_of: "2026-09-11T12:00:00Z",
    actual: {
      round_trips: [
        {
          symbol: "AAPL.US",
          quantity: "92",
          entry_price: "108.000000",
          entered_at: "2026-02-02T21:00:00Z",
          exit_price: "112.500000",
          exited_at: "2026-02-05T21:00:00Z",
          realized_pnl: "414.00",
          return_pct: "4.1667",
        },
      ],
      open_positions: { "MSFT.US": "50" },
      num_round_trips: 1,
      num_winning: 1,
      win_rate_pct: "100.0000",
      total_realized_pnl: "414.00",
      avg_return_pct: "4.1667",
    },
    expected: {
      status: "available",
      reference_backtest_run_id: "11111111-2222-3333-4444-555555555555",
      symbol: "AAPL.US",
      total_return_pct: "12.5000",
      max_drawdown_pct: "-6.2000",
      win_rate_pct: "55.0000",
      num_trades: 20,
    },
    ...overrides,
  };
}

function mockOnce(status: number, body: unknown, ok = status < 400) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok,
    status,
    json: async () => body,
  });
}

describe("DeploymentMonitoringPanel", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders both the expected and actual blocks with real numbers from a full example response", async () => {
    mockOnce(200, monitoring());
    render(<DeploymentMonitoringPanel deploymentId="dep-1" />);

    await waitFor(() =>
      expect(screen.getByTestId("expected-total-return")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("expected-total-return")).toHaveTextContent(
      "12.5000%",
    );
    expect(screen.getByTestId("expected-max-drawdown")).toHaveTextContent(
      "-6.2000%",
    );
    expect(screen.getByTestId("expected-win-rate")).toHaveTextContent(
      "55.0000%",
    );
    expect(screen.getByTestId("expected-num-trades")).toHaveTextContent("20");
    expect(screen.getByTestId("expected-symbol")).toHaveTextContent("AAPL.US");
    expect(screen.getByTestId("expected-reference-id")).toHaveTextContent(
      "11111111",
    );

    expect(screen.getByTestId("actual-num-round-trips")).toHaveTextContent("1");
    expect(screen.getByTestId("actual-win-rate")).toHaveTextContent(
      "100.0000%",
    );
    expect(screen.getByTestId("actual-realized-pnl")).toHaveTextContent(
      "414.00",
    );
    expect(screen.getByTestId("actual-avg-return")).toHaveTextContent(
      "4.1667%",
    );

    const row = screen.getByTestId("deployment-round-trip-row");
    expect(row).toHaveTextContent("AAPL.US");
    expect(row).toHaveTextContent("92");
    expect(row).toHaveTextContent("108.000000");
    expect(row).toHaveTextContent("112.500000");
    expect(row).toHaveTextContent("414.00");
    expect(row).toHaveTextContent("4.1667%");
  });

  it("shows the explanatory message and no stat block when expected.status is no_reference_backtest", async () => {
    mockOnce(
      200,
      monitoring({
        expected: {
          status: "no_reference_backtest",
          reference_backtest_run_id: null,
          symbol: null,
          total_return_pct: null,
          max_drawdown_pct: null,
          win_rate_pct: null,
          num_trades: null,
        },
      }),
    );
    render(<DeploymentMonitoringPanel deploymentId="dep-1" />);

    await waitFor(() =>
      expect(
        screen.getByText(/no backtest exists yet for this strategy version/i),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("expected-total-return")).not.toBeInTheDocument();
    expect(screen.queryByTestId("expected-reference-id")).not.toBeInTheDocument();
  });

  it("shows an em-dash for win rate / avg return and an empty-state row when num_round_trips is 0, never 0 or blank", async () => {
    mockOnce(
      200,
      monitoring({
        actual: {
          round_trips: [],
          open_positions: {},
          num_round_trips: 0,
          num_winning: 0,
          win_rate_pct: null,
          total_realized_pnl: "0.00",
          avg_return_pct: null,
        },
      }),
    );
    render(<DeploymentMonitoringPanel deploymentId="dep-1" />);

    await waitFor(() =>
      expect(screen.getByTestId("actual-num-round-trips")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("actual-win-rate")).toHaveTextContent("—");
    expect(screen.getByTestId("actual-win-rate")).not.toHaveTextContent("0%");
    expect(screen.getByTestId("actual-avg-return")).toHaveTextContent("—");
    expect(screen.getByTestId("actual-avg-return")).not.toHaveTextContent("0%");

    expect(screen.getByTestId("actual-round-trips-empty")).toHaveTextContent(
      /no closed trades yet/i,
    );
    expect(
      screen.queryByTestId("deployment-round-trip-row"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("actual-open-positions"),
    ).not.toBeInTheDocument();
  });

  it("renders open_positions when non-empty and omits the list when {}", async () => {
    mockOnce(200, monitoring());
    render(<DeploymentMonitoringPanel deploymentId="dep-1" />);

    await waitFor(() =>
      expect(screen.getByTestId("actual-open-positions")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("actual-open-positions")).toHaveTextContent(
      "MSFT.US: 50 shares open",
    );
  });
});
