import { describe, it, expect } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { SignalTable } from "@/components/SignalTable";
import type { SignalEvaluationResponse } from "@/components/CheckSignalsForm";

function evaluation(
  overrides: Partial<SignalEvaluationResponse> = {},
): SignalEvaluationResponse {
  return {
    id: `eval-${overrides.symbol ?? "x"}`,
    strategy_version_id: "v-1",
    symbol: "AAPL.US",
    bar_interval: "1d",
    as_of_bar_date: "2026-09-09",
    latest_close: "108.00",
    signal: "buy",
    entry_rule_held: true,
    exit_rule_held: false,
    insufficient_data: false,
    indicator_values: { sma_20: "105.50", rsi_14: "58.20" },
    explanation:
      "close (108.00) crossed above sma_20 (105.50) on 2026-09-09 -> BUY",
    created_at: "2026-09-09T00:00:00Z",
    ...overrides,
  };
}

describe("SignalTable", () => {
  it("renders EmptyNote for an empty list", () => {
    render(<SignalTable evaluations={[]} />);
    expect(screen.getByText(/no signal evaluations yet/i)).toBeInTheDocument();
    expect(screen.queryByTestId("signal-row")).not.toBeInTheDocument();
  });

  it("renders buy / sell / hold rows with the right pill tone", () => {
    render(
      <SignalTable
        evaluations={[
          evaluation({ symbol: "BUY.US", signal: "buy" }),
          evaluation({ symbol: "SELL.US", signal: "sell" }),
          evaluation({ symbol: "HOLD.US", signal: "hold" }),
        ]}
      />,
    );
    expect(screen.getByTestId("signal-pill-BUY.US")).toHaveTextContent("buy");
    expect(screen.getByTestId("signal-pill-BUY.US").className).toContain("text-pos");
    expect(screen.getByTestId("signal-pill-SELL.US").className).toContain("text-neg");
    expect(screen.getByTestId("signal-pill-HOLD.US").className).toContain(
      "text-ink-muted",
    );
  });

  it("always renders the full explanation string verbatim", () => {
    const explanation =
      "only 8 of the 20 bars sma_20 needs were available (2026-08-30 to 2026-09-09), so the entry rule could not be evaluated -> HOLD";
    render(
      <SignalTable
        evaluations={[evaluation({ symbol: "AAPL.US", explanation })]}
      />,
    );
    expect(screen.getByTestId("explanation-AAPL.US")).toHaveTextContent(explanation);
  });

  it("renders an insufficient_data row honestly: HOLD + note + em-dashes, not an error", () => {
    render(
      <SignalTable
        evaluations={[
          evaluation({
            symbol: "THIN.US",
            signal: "hold",
            insufficient_data: true,
            as_of_bar_date: "2026-09-09",
            latest_close: null,
            entry_rule_held: null,
            exit_rule_held: null,
            indicator_values: { sma_20: null },
            explanation:
              "only 8 of the 20 bars sma_20 needs were available -> HOLD",
          }),
        ]}
      />,
    );

    expect(screen.getByTestId("signal-pill-THIN.US")).toHaveTextContent("hold");
    expect(screen.getByTestId("insufficient-THIN.US")).toHaveTextContent(
      /insufficient data/i,
    );
    expect(screen.getByTestId("close-THIN.US")).toHaveTextContent("—");
    expect(screen.getByTestId("close-THIN.US")).not.toHaveTextContent("0");
    expect(screen.getByTestId("explanation-THIN.US")).toHaveTextContent(
      "only 8 of the 20 bars sma_20 needs were available -> HOLD",
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("signal-toggle-THIN.US"));
    expect(screen.getByTestId("indicator-THIN.US-sma_20")).toHaveTextContent("—");
    expect(screen.getByTestId("entry-held-THIN.US")).toHaveTextContent(
      "entry_rule_held: could not evaluate",
    );
    expect(screen.getByTestId("exit-held-THIN.US")).toHaveTextContent(
      "exit_rule_held: could not evaluate",
    );
  });

  it("expands a row to show indicator values (with — for a null) and yes/no rule checks", () => {
    render(
      <SignalTable
        evaluations={[
          evaluation({
            symbol: "AAPL.US",
            entry_rule_held: true,
            exit_rule_held: false,
            indicator_values: { sma_20: "105.50", rsi_14: null },
          }),
        ]}
      />,
    );

    const detail = screen.getByTestId("signal-detail-AAPL.US");
    expect(detail).not.toBeVisible();

    fireEvent.click(screen.getByTestId("signal-toggle-AAPL.US"));

    expect(detail).toBeVisible();
    expect(
      within(detail).getByTestId("indicator-AAPL.US-sma_20"),
    ).toHaveTextContent("105.50");
    expect(
      within(detail).getByTestId("indicator-AAPL.US-rsi_14"),
    ).toHaveTextContent("—");
    expect(within(detail).getByTestId("entry-held-AAPL.US")).toHaveTextContent(
      "entry_rule_held: yes",
    );
    expect(within(detail).getByTestId("exit-held-AAPL.US")).toHaveTextContent(
      "exit_rule_held: no",
    );
  });
});
