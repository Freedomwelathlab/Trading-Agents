import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import SignalFilter, { DEFAULT_SIGNAL_FILTER } from "@/components/markets/SignalFilter";
import SignalScoreTable from "@/components/markets/SignalScoreTable";

describe("SignalFilter", () => {
  it("defaults to the operator's bar: score 7+ and 70% confidence", () => {
    expect(DEFAULT_SIGNAL_FILTER).toEqual({ scores: [7, 8, 9, 10], minConfidence: 70 });
    render(<SignalFilter value={DEFAULT_SIGNAL_FILTER} onChange={() => {}} />);
    expect(screen.getByTestId("signal-filter-button")).toHaveTextContent("score 7,8,9,10 · ≥70%");
  });

  it("ticking a score adds it to the filter", () => {
    const onChange = vi.fn();
    render(<SignalFilter value={DEFAULT_SIGNAL_FILTER} onChange={onChange} />);
    fireEvent.click(screen.getByTestId("signal-filter-button"));
    fireEvent.click(screen.getByLabelText("Score 5"));
    expect(onChange).toHaveBeenCalledWith({ scores: [5, 7, 8, 9, 10], minConfidence: 70 });
  });

  it("changing the confidence floor reports it", () => {
    const onChange = vi.fn();
    render(<SignalFilter value={DEFAULT_SIGNAL_FILTER} onChange={onChange} />);
    fireEvent.click(screen.getByTestId("signal-filter-button"));
    fireEvent.change(screen.getByLabelText("Minimum confidence"), { target: { value: "55" } });
    expect(onChange).toHaveBeenCalledWith({ scores: [7, 8, 9, 10], minConfidence: 55 });
  });
});

describe("SignalScoreTable empty state", () => {
  it("says the filter hid signals rather than that nothing happened", () => {
    render(
      <SignalScoreTable
        signals={[]}
        found={719}
        maxScore={6}
        maxConfidence="70.6"
        filter={DEFAULT_SIGNAL_FILTER}
      />,
    );
    expect(screen.getByText(/found 719 signal\(s\)/)).toBeInTheDocument();
    expect(screen.getByText(/highest score was 6/)).toBeInTheDocument();
    expect(screen.getByText(/70\.6%/)).toBeInTheDocument();
  });

  it("shows a measured confidence per row", () => {
    render(
      <SignalScoreTable
        signals={[
          {
            ts: "2026-09-25T14:00:00Z",
            setup: "fib_confluence",
            side: "B",
            price: "79.1",
            stop_price: "78.6",
            score: 3,
            evidence: {},
            confidence: "70.6",
            confidence_sample: 34,
          },
        ]}
      />,
    );
    expect(screen.getByText("70.6%")).toBeInTheDocument();
  });
});
