import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { DrawdownChart } from "@/components/DrawdownChart";

/**
 * See EquityCurveChart.test.tsx for why this stub is needed under jsdom,
 * and why only the `ResponsiveContainer`'s own div is given a real size.
 */
function mockResponsiveContainer() {
  global.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;

  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
    this: HTMLElement,
  ) {
    if (this.classList.contains("recharts-responsive-container")) {
      return {
        width: 600,
        height: 220,
        top: 0,
        left: 0,
        bottom: 220,
        right: 600,
        x: 0,
        y: 0,
        toJSON() {},
      } as DOMRect;
    }
    return {
      width: 0,
      height: 0,
      top: 0,
      left: 0,
      bottom: 0,
      right: 0,
      x: 0,
      y: 0,
      toJSON() {},
    } as DOMRect;
  });
}

describe("DrawdownChart", () => {
  beforeEach(() => {
    mockResponsiveContainer();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the empty state for no points", () => {
    render(<DrawdownChart points={[]} />);
    expect(screen.getByText(/no drawdown to chart/i)).toBeInTheDocument();
  });

  it("renders a single filled area for the real drawdown series", () => {
    const { container } = render(
      <DrawdownChart
        points={[
          { date: "2026-01-01", drawdownPct: 0 },
          { date: "2026-01-02", drawdownPct: 12.5 },
          { date: "2026-01-03", drawdownPct: 4 },
        ]}
      />,
    );
    expect(screen.getByTestId("drawdown-chart")).toBeInTheDocument();
    expect(container.querySelectorAll(".recharts-area")).toHaveLength(1);
    // The axis label states the sign convention unambiguously rather than
    // leaving the negative-going display to be inferred.
    expect(screen.getByText(/drawdown from peak/i)).toBeInTheDocument();
  });
});
