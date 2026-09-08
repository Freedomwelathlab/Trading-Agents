import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { BacktestRunList } from "@/components/BacktestRunList";
import type { BacktestRunSummary } from "@/components/RunBacktestForm";

function succeededRun(overrides: Partial<BacktestRunSummary> = {}): BacktestRunSummary {
  return {
    id: "run-1",
    strategy_version_id: "v-1",
    symbol: "AAPL.US",
    bar_interval: "1d",
    start_date: "2026-01-01",
    end_date: "2026-06-30",
    starting_cash: "100000",
    status: "succeeded",
    final_equity: "112450.00",
    total_return_pct: "12.45",
    max_drawdown_pct: "6.10",
    win_rate_pct: "60.00",
    num_trades: 5,
    error_detail: null,
    created_at: "2026-09-01T00:00:00Z",
    completed_at: "2026-09-01T00:01:00Z",
    ...overrides,
  };
}

function failedRun(overrides: Partial<BacktestRunSummary> = {}): BacktestRunSummary {
  return {
    id: "run-2",
    strategy_version_id: "v-1",
    symbol: "MSFT.US",
    bar_interval: "1d",
    start_date: "2026-02-01",
    end_date: "2026-03-01",
    starting_cash: "100000",
    status: "failed",
    final_equity: null,
    total_return_pct: null,
    max_drawdown_pct: null,
    win_rate_pct: null,
    num_trades: null,
    error_detail: "DATA_UNAVAILABLE: vendor request failed.",
    created_at: "2026-09-01T00:00:00Z",
    completed_at: "2026-09-01T00:01:00Z",
    ...overrides,
  };
}

describe("BacktestRunList", () => {
  it("renders a mixed succeeded/failed run list, with failed metrics shown as em-dashes, never 0%", () => {
    const ok = succeededRun();
    const bad = failedRun();
    render(
      <BacktestRunList
        strategyId="s-1"
        versionId="v-1"
        runs={[ok, bad]}
        onSelectionChange={vi.fn()}
      />,
    );

    expect(screen.getAllByTestId("backtest-run-row")).toHaveLength(2);

    expect(screen.getByTestId(`return-${ok.id}`)).toHaveTextContent("12.45");
    expect(screen.getByTestId(`drawdown-${ok.id}`)).toHaveTextContent("6.10");
    expect(screen.getByTestId(`winrate-${ok.id}`)).toHaveTextContent("60.00");

    expect(screen.getByTestId(`return-${bad.id}`)).toHaveTextContent("—");
    expect(screen.getByTestId(`return-${bad.id}`)).not.toHaveTextContent("0%");
    expect(screen.getByTestId(`drawdown-${bad.id}`)).toHaveTextContent("—");
    expect(screen.getByTestId(`winrate-${bad.id}`)).toHaveTextContent("—");
  });

  it("renders the EmptyNote, not an empty table shell, when there are no runs", () => {
    render(
      <BacktestRunList strategyId="s-1" versionId="v-1" runs={[]} onSelectionChange={vi.fn()} />,
    );

    expect(screen.getByText(/no backtest runs yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("reports checkbox selection with the real run ids, added and removed", () => {
    const ok = succeededRun();
    const bad = failedRun();
    const onSelectionChange = vi.fn();
    render(
      <BacktestRunList
        strategyId="s-1"
        versionId="v-1"
        runs={[ok, bad]}
        onSelectionChange={onSelectionChange}
      />,
    );

    fireEvent.click(screen.getByRole("checkbox", { name: `Compare run ${ok.id}` }));
    expect(onSelectionChange).toHaveBeenLastCalledWith([ok.id]);

    fireEvent.click(screen.getByRole("checkbox", { name: `Compare run ${bad.id}` }));
    expect(onSelectionChange).toHaveBeenLastCalledWith([ok.id, bad.id]);

    fireEvent.click(screen.getByRole("checkbox", { name: `Compare run ${ok.id}` }));
    expect(onSelectionChange).toHaveBeenLastCalledWith([bad.id]);
  });

  it("links each row's View action to the real strategy/run detail path", () => {
    const ok = succeededRun({ id: "run-abc" });
    render(
      <BacktestRunList
        strategyId="strat-42"
        versionId="v-1"
        runs={[ok]}
        onSelectionChange={vi.fn()}
      />,
    );

    const link = screen.getByRole("link", { name: /view/i });
    expect(link).toHaveAttribute("href", "/strategies/strat-42/backtests/run-abc");
  });
});
