import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MonthlyReturnsHeatmap } from "@/components/MonthlyReturnsHeatmap";

describe("MonthlyReturnsHeatmap", () => {
  it("renders the empty state for no months", () => {
    render(<MonthlyReturnsHeatmap months={[]} />);
    expect(screen.getByText(/no monthly returns to show/i)).toBeInTheDocument();
  });

  it("renders one row per real year and a dash for a year-month with no entry", () => {
    render(
      <MonthlyReturnsHeatmap
        months={[
          { year: 2026, month: 1, returnPct: 5.25 },
          { year: 2026, month: 3, returnPct: -2.5 },
        ]}
      />,
    );

    expect(screen.getByTestId("monthly-returns-heatmap")).toBeInTheDocument();
    // Real figures render with their sign and magnitude.
    expect(screen.getByTestId("heatmap-cell-2026-1")).toHaveTextContent("5.3%");
    expect(screen.getByTestId("heatmap-cell-2026-3")).toHaveTextContent("-2.5%");
    // February has no entry in `months` — a dash, never a fabricated 0%.
    expect(screen.getByTestId("heatmap-cell-2026-2")).toHaveTextContent("—");
    // Only one real year is present, so only one data row.
    expect(screen.getAllByText("2026")).toHaveLength(1);
  });

  it("colours a positive month with the pos tone and a negative month with the neg tone", () => {
    render(
      <MonthlyReturnsHeatmap
        months={[
          { year: 2026, month: 1, returnPct: 8 },
          { year: 2026, month: 2, returnPct: -8 },
        ]}
      />,
    );
    expect(screen.getByTestId("heatmap-cell-2026-1")).toHaveClass("text-pos");
    expect(screen.getByTestId("heatmap-cell-2026-2")).toHaveClass("text-neg");
  });

  it("renders multiple real years as separate rows, sorted", () => {
    render(
      <MonthlyReturnsHeatmap
        months={[
          { year: 2027, month: 1, returnPct: 1 },
          { year: 2026, month: 1, returnPct: 2 },
        ]}
      />,
    );
    const rows = screen.getAllByRole("row");
    // Header row + one row per year (2026, 2027).
    expect(rows).toHaveLength(3);
  });
});
