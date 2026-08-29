import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import PortfolioHistoryChart, { buildPoints } from "@/components/PortfolioHistoryChart";
import { sessionNavigation } from "@/lib/session";

function entry(id: string, capturedAt: string, equity: string) {
  return {
    id,
    broker_id: "b-1",
    captured_at: capturedAt,
    cash: "1000.00",
    positions: [],
    total_equity: equity,
    total_unrealized_pnl: "0.00",
    total_realized_pnl: "0.00",
  };
}

function mockJson(status: number, body: unknown, ok = status < 400) {
  (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok,
    status,
    json: async () => body,
  });
}

function submit(brokerId = "b-1") {
  fireEvent.change(screen.getByLabelText(/broker id/i), { target: { value: brokerId } });
  fireEvent.click(screen.getByRole("button", { name: /load history/i }));
}

describe("PortfolioHistoryChart", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("charts the real snapshots the backend returned", async () => {
    mockJson(200, {
      snapshots: [
        entry("s-1", "2026-08-01T00:00:00Z", "10000.00"),
        entry("s-2", "2026-08-02T00:00:00Z", "10500.00"),
        entry("s-3", "2026-08-03T00:00:00Z", "9800.00"),
      ],
      limit: 50,
      offset: 0,
    });

    render(<PortfolioHistoryChart />);
    submit();

    await waitFor(() => expect(screen.getByTestId("equity-line")).toBeInTheDocument());
    // One plotted vertex per real snapshot — nothing interpolated.
    expect(
      screen.getByTestId("equity-line").getAttribute("points")?.split(" "),
    ).toHaveLength(3);
    expect(screen.getByText("10500.00")).toBeInTheDocument();
    // Once in the SVG point tooltip, once in the accompanying table.
    expect(screen.getAllByText(/2026-08-03T00:00:00Z/).length).toBeGreaterThanOrEqual(1);
  });

  it("says nothing has been captured rather than drawing an empty curve", async () => {
    mockJson(200, { snapshots: [], limit: 50, offset: 0 });

    render(<PortfolioHistoryChart />);
    submit();

    await waitFor(() =>
      expect(screen.getByText(/No snapshots have been captured/i)).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("equity-line")).not.toBeInTheDocument();
  });

  it("renders a real 403 for a caller without the portfolio permission or grant", async () => {
    mockJson(403, { detail: "Missing required permission: portfolio:view" }, false);

    render(<PortfolioHistoryChart />);
    submit();

    await waitFor(() =>
      expect(screen.getByText(/portfolio:view/)).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("equity-line")).not.toBeInTheDocument();
  });

  it("renders a real 404 for an unknown broker id", async () => {
    mockJson(404, { detail: "No broker with id b-unknown." }, false);

    render(<PortfolioHistoryChart />);
    submit("b-unknown");

    await waitFor(() =>
      expect(screen.getByText(/No broker with id b-unknown/)).toBeInTheDocument(),
    );
  });

  it("surfaces the backend's DATA_UNAVAILABLE sentinel verbatim", async () => {
    mockJson(400, { detail: "DATA_UNAVAILABLE: no mark supplied for AAPL.US" }, false);

    render(<PortfolioHistoryChart />);
    submit();

    await waitFor(() =>
      expect(
        screen.getByText(/DATA_UNAVAILABLE: no mark supplied for AAPL\.US/),
      ).toBeInTheDocument(),
    );
  });

  it("shows a real error state on network failure", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("down"));

    render(<PortfolioHistoryChart />);
    submit();

    await waitFor(() => expect(screen.getByText(/DATA_UNAVAILABLE/)).toBeInTheDocument());
  });

  it("redirects to login on a 401 instead of showing a raw error", async () => {
    const toLogin = vi.spyOn(sessionNavigation, "toLogin").mockImplementation(() => {});
    mockJson(401, { detail: "Not authenticated" }, false);

    render(<PortfolioHistoryChart />);
    submit();

    await waitFor(() => expect(toLogin).toHaveBeenCalledWith("session-expired"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("buildPoints", () => {
  it("maps the highest equity to the top of the plot and the lowest to the bottom", () => {
    const { points, min, max } = buildPoints([
      entry("a", "t1", "100"),
      entry("b", "t2", "300"),
      entry("c", "t3", "200"),
    ]);
    expect(min).toBe(100);
    expect(max).toBe(300);
    expect(points[1].y).toBeLessThan(points[2].y);
    expect(points[2].y).toBeLessThan(points[0].y);
    // x is strictly increasing in capture order.
    expect(points[0].x).toBeLessThan(points[1].x);
    expect(points[1].x).toBeLessThan(points[2].x);
  });

  it("draws a flat series as a level line instead of dividing by zero", () => {
    const { points } = buildPoints([entry("a", "t1", "500"), entry("b", "t2", "500")]);
    expect(points.every((p) => Number.isFinite(p.y))).toBe(true);
    expect(points[0].y).toBe(points[1].y);
  });

  it("centres a single snapshot rather than producing NaN", () => {
    const { points } = buildPoints([entry("a", "t1", "42")]);
    expect(points).toHaveLength(1);
    expect(Number.isFinite(points[0].x)).toBe(true);
    expect(Number.isFinite(points[0].y)).toBe(true);
  });
});
