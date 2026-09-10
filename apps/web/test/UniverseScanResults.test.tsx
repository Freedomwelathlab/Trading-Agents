import { describe, it, expect } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { UniverseScanResults } from "@/components/UniverseScanResults";
import type {
  UniverseScanDetailResponse,
  UniverseScanResultResponse,
} from "@/components/UniverseScanForm";

function result(
  overrides: Partial<UniverseScanResultResponse> = {},
): UniverseScanResultResponse {
  return {
    id: `res-${overrides.symbol ?? "x"}`,
    symbol: "AAPL.US",
    backtest_run_id: "run-aapl",
    status: "succeeded",
    total_return_pct: "12.45",
    max_drawdown_pct: "6.10",
    win_rate_pct: "60.00",
    num_trades: 5,
    error_detail: null,
    rank: 1,
    ...overrides,
  };
}

function scan(
  overrides: Partial<UniverseScanDetailResponse> = {},
): UniverseScanDetailResponse {
  return {
    id: "scan-1",
    strategy_version_id: "v-1",
    bar_interval: "1d",
    start_date: "2026-01-01",
    end_date: "2026-06-30",
    starting_cash: "100000",
    scan_mode: "explicit_list",
    requested_symbols: ["AAPL.US", "MSFT.US", "ZZZ.US"],
    status: "succeeded",
    num_symbols: 3,
    num_succeeded: 2,
    num_qualified: 1,
    error_detail: null,
    created_at: "2026-09-01T00:00:00Z",
    completed_at: "2026-09-01T00:05:00Z",
    results: [],
    ...overrides,
  };
}

describe("UniverseScanResults", () => {
  it("renders the ranked results in backend order, failed rows at the bottom with em-dashes", () => {
    const rows = [
      result({ symbol: "AAPL.US", rank: 1, total_return_pct: "12.45", backtest_run_id: "run-aapl" }),
      result({ symbol: "MSFT.US", rank: 2, total_return_pct: "-3.20", backtest_run_id: "run-msft" }),
      result({
        symbol: "ZZZ.US",
        rank: null,
        status: "failed",
        total_return_pct: null,
        max_drawdown_pct: null,
        win_rate_pct: null,
        num_trades: null,
        error_detail: "DATA_UNAVAILABLE: no ingested bars for ZZZ.US over the window.",
        backtest_run_id: "run-zzz",
      }),
    ];
    render(<UniverseScanResults scan={scan({ results: rows })} strategyId="s-1" />);

    const tableRows = screen.getAllByTestId("universe-scan-result-row");
    expect(tableRows).toHaveLength(3);
    expect(within(tableRows[0]).getByText("AAPL.US")).toBeInTheDocument();
    expect(within(tableRows[2]).getByText("ZZZ.US")).toBeInTheDocument();

    expect(screen.getByTestId("rank-AAPL.US")).toHaveTextContent("#1");
    expect(screen.getByTestId("rank-ZZZ.US")).toHaveTextContent("—");
    expect(screen.getByTestId("return-ZZZ.US")).toHaveTextContent("—");
    expect(screen.getByTestId("return-ZZZ.US")).not.toHaveTextContent("0");
    expect(screen.getByTestId("trades-ZZZ.US")).toHaveTextContent("—");
    expect(screen.getByTestId("result-error-ZZZ.US")).toHaveTextContent(
      "DATA_UNAVAILABLE: no ingested bars for ZZZ.US over the window.",
    );
  });

  it("pnl-tone-colours the return cell from the sign of the number", () => {
    const rows = [
      result({ symbol: "UP.US", total_return_pct: "12.45" }),
      result({ symbol: "DOWN.US", rank: 2, total_return_pct: "-3.20" }),
    ];
    render(<UniverseScanResults scan={scan({ results: rows })} strategyId="s-1" />);

    expect(screen.getByTestId("return-UP.US").className).toContain("text-pos");
    expect(screen.getByTestId("return-DOWN.US").className).toContain("text-neg");
  });

  it("links each row to the real Phase 56 backtest-run detail page", () => {
    const rows = [result({ symbol: "AAPL.US", backtest_run_id: "run-abc" })];
    render(<UniverseScanResults scan={scan({ results: rows })} strategyId="strat-9" />);

    const link = screen.getByRole("link", { name: /backtest/i });
    expect(link).toHaveAttribute("href", "/strategies/strat-9/backtests/run-abc");
  });

  it("renders an honest note, not an error, for a succeeded scan with num_succeeded: 0", () => {
    const rows = [
      result({
        symbol: "AAPL.US",
        rank: null,
        status: "failed",
        total_return_pct: null,
        max_drawdown_pct: null,
        win_rate_pct: null,
        num_trades: null,
        error_detail: "DATA_UNAVAILABLE: no ingested bars over the window.",
      }),
    ];
    render(
      <UniverseScanResults
        scan={scan({ status: "succeeded", num_succeeded: 0, num_qualified: 0, results: rows })}
        strategyId="s-1"
      />,
    );

    expect(screen.getByTestId("scan-summary")).toHaveTextContent(
      /none of the 3 symbols had enough history/i,
    );
    expect(screen.queryByTestId("scan-error-detail")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    // The results table still lists every symbol.
    expect(screen.getAllByTestId("universe-scan-result-row")).toHaveLength(1);
  });

  it("renders error_detail in an Alert and no results table for a failed scan", () => {
    render(
      <UniverseScanResults
        scan={scan({
          status: "failed",
          num_symbols: null,
          num_succeeded: null,
          num_qualified: null,
          error_detail: "DATA_UNAVAILABLE: could not reach the backtest engine.",
          results: [],
        })}
        strategyId="s-1"
      />,
    );

    expect(screen.getByTestId("scan-error-detail")).toHaveTextContent(
      "DATA_UNAVAILABLE: could not reach the backtest engine.",
    );
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});
