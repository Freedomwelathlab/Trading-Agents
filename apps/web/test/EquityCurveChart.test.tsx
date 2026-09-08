import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { EquityCurveChart, buildEquityChartData } from "@/components/EquityCurveChart";

/**
 * Recharts' `ResponsiveContainer` sizes itself off a `ResizeObserver` and
 * the container's real layout — neither exists in jsdom. Stubbing both to
 * report a real, positive size (rather than jsdom's default 0x0) lets the
 * chart mount exactly as it would in a real browser, instead of skipping
 * this dependency's very first use in the codebase.
 *
 * The size is reported ONLY for the `ResponsiveContainer`'s own outer div
 * — every other element (in particular the Legend's wrapper, when more
 * than one series is rendered) reports 0x0, its jsdom default. Recharts
 * subtracts the Legend's measured height from the total to get the plot
 * area's height; if the mock also told the Legend it was 280px tall, that
 * would consume the whole chart and squeeze the plotted lines to zero
 * height, which is what happened before the check was scoped this way.
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
        height: 280,
        top: 0,
        left: 0,
        bottom: 280,
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

describe("buildEquityChartData", () => {
  it("aligns multiple series by date, leaving a series' key absent where it has no point that day", () => {
    const data = buildEquityChartData([
      {
        name: "A",
        points: [
          { date: "2026-01-01", equity: 100 },
          { date: "2026-01-02", equity: 105 },
        ],
      },
      { name: "B", points: [{ date: "2026-01-02", equity: 200 }] },
    ]);
    expect(data).toEqual([
      { date: "2026-01-01", A: 100 },
      { date: "2026-01-02", A: 105, B: 200 },
    ]);
  });

  it("returns an empty array for no series", () => {
    expect(buildEquityChartData([])).toEqual([]);
  });
});

describe("EquityCurveChart", () => {
  beforeEach(() => {
    mockResponsiveContainer();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the empty state when every series has no points", () => {
    render(<EquityCurveChart series={[{ name: "AAPL.US", points: [] }]} />);
    expect(screen.getByText(/no equity curve to chart/i)).toBeInTheDocument();
  });

  it("renders the empty state for an empty series list", () => {
    render(<EquityCurveChart series={[]} />);
    expect(screen.getByText(/no equity curve to chart/i)).toBeInTheDocument();
  });

  it("renders one line per real series and a legend only once there is more than one", () => {
    const { container, rerender } = render(
      <EquityCurveChart
        series={[
          {
            name: "AAPL.US",
            points: [
              { date: "2026-01-01", equity: 100 },
              { date: "2026-01-02", equity: 110 },
            ],
          },
        ]}
      />,
    );
    expect(screen.getByTestId("equity-curve-chart")).toBeInTheDocument();
    expect(container.querySelectorAll(".recharts-line")).toHaveLength(1);
    expect(container.querySelector(".recharts-legend-wrapper")).not.toBeInTheDocument();

    rerender(
      <EquityCurveChart
        series={[
          {
            name: "AAPL.US",
            points: [
              { date: "2026-01-01", equity: 100 },
              { date: "2026-01-02", equity: 105 },
            ],
          },
          {
            name: "MSFT.US",
            points: [
              { date: "2026-01-01", equity: 200 },
              { date: "2026-01-02", equity: 210 },
            ],
          },
        ]}
      />,
    );
    expect(container.querySelectorAll(".recharts-line")).toHaveLength(2);
    expect(container.querySelector(".recharts-legend-wrapper")).toBeInTheDocument();
    expect(screen.getByText("AAPL.US")).toBeInTheDocument();
    expect(screen.getByText("MSFT.US")).toBeInTheDocument();
  });
});
