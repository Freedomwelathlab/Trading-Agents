import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { BacktestTradeLedger, type TradeRow } from "@/components/BacktestTradeLedger";

function trade(overrides: Partial<TradeRow> = {}): TradeRow {
  return {
    side: "buy",
    entry_date: "2026-01-05",
    entry_price: "101.20",
    exit_date: "2026-01-20",
    exit_price: "108.00",
    quantity: "50",
    return_pct: "6.72",
    ...overrides,
  };
}

describe("BacktestTradeLedger", () => {
  it("renders the empty state for no trades", () => {
    render(<BacktestTradeLedger trades={[]} />);
    expect(screen.getByText(/completed no round trips/i)).toBeInTheDocument();
    expect(screen.queryByTestId("trade-row")).not.toBeInTheDocument();
  });

  it("renders every real trade with all seven columns", () => {
    render(<BacktestTradeLedger trades={[trade()]} />);
    const row = screen.getByTestId("trade-row");
    expect(row).toHaveTextContent("buy");
    expect(row).toHaveTextContent("2026-01-05");
    expect(row).toHaveTextContent("101.20");
    expect(row).toHaveTextContent("2026-01-20");
    expect(row).toHaveTextContent("108.00");
    expect(row).toHaveTextContent("50");
    expect(row).toHaveTextContent("6.72");
  });

  it("colours a positive return_pct with the pos tone and a negative one with the neg tone", () => {
    render(
      <BacktestTradeLedger
        trades={[trade({ return_pct: "6.72" }), trade({ return_pct: "-3.10" })]}
      />,
    );
    expect(screen.getByTestId("trade-return-0")).toHaveClass("text-pos");
    expect(screen.getByTestId("trade-return-1")).toHaveClass("text-neg");
  });

  it("gives a zero return_pct no colour at all, same as the rest of this dashboard's returnTone convention", () => {
    render(<BacktestTradeLedger trades={[trade({ return_pct: "0" })]} />);
    const cell = screen.getByTestId("trade-return-0");
    expect(cell).not.toHaveClass("text-pos");
    expect(cell).not.toHaveClass("text-neg");
    expect(cell).toHaveTextContent("0");
  });
});
