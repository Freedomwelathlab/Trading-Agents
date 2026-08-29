import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import PortfolioView from "@/components/PortfolioView";

describe("PortfolioView", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function fillRequiredFields() {
    fireEvent.change(screen.getByLabelText(/broker id/i), {
      target: { value: "11111111-1111-1111-1111-111111111111" },
    });
  }

  it("renders a real portfolio snapshot: cash, positions, and totals", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        broker_id: "11111111-1111-1111-1111-111111111111",
        cash: "99000.00000000",
        positions: [
          {
            symbol: "AAPL.US",
            quantity: "10.00000000",
            avg_cost: "100.00000000",
            current_value: "1200.00000000",
            unrealized_pnl: "200.0000000000000000",
            realized_pnl: "0",
          },
        ],
        total_equity: "100200.00000000",
        total_unrealized_pnl: "200.0000000000000000",
        total_realized_pnl: "50.0000000000000000",
      }),
    });

    render(<PortfolioView />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /view portfolio/i }));

    await waitFor(() => expect(screen.getByText("99000.00000000")).toBeInTheDocument());
    expect(screen.getByText("AAPL.US")).toBeInTheDocument();
    expect(screen.getByText("100.00000000")).toBeInTheDocument();
    expect(screen.getByText("1200.00000000")).toBeInTheDocument();
    expect(screen.getByText("100200.00000000")).toBeInTheDocument();
    expect(screen.getByText("50.0000000000000000")).toBeInTheDocument();
    expect(screen.getAllByText("200.0000000000000000").length).toBeGreaterThan(0);
  });

  it("renders 'No open positions' when the positions array is empty", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        broker_id: "11111111-1111-1111-1111-111111111111",
        cash: "100000.00000000",
        positions: [],
        total_equity: "100000.00000000",
        total_unrealized_pnl: "0",
        total_realized_pnl: "0",
      }),
    });

    render(<PortfolioView />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /view portfolio/i }));

    await waitFor(() => expect(screen.getByText(/no open positions/i)).toBeInTheDocument());
  });

  it("surfaces the real 400 DATA_UNAVAILABLE sentinel for a missing mark, never a guessed price", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 400,
      json: async () => ({
        detail: "DATA_UNAVAILABLE: no mark provided for held symbol AAPL.US",
      }),
    });

    render(<PortfolioView />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /view portfolio/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/DATA_UNAVAILABLE: no mark provided for held symbol AAPL.US/),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText(/HTTP 400/)).toBeInTheDocument();
  });

  it("surfaces a real 403 for missing VIEW_PORTFOLIO permission or no broker grant", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 403,
      json: async () => ({
        detail: "Missing required permission: portfolio:view",
      }),
    });

    render(<PortfolioView />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /view portfolio/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/Missing required permission: portfolio:view/),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText(/HTTP 403/)).toBeInTheDocument();
  });

  it("surfaces a real 404 for an unknown broker_id, never a fabricated empty portfolio", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: async () => ({
        detail: "No broker with id 99999999-9999-9999-9999-999999999999.",
      }),
    });

    render(<PortfolioView />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /view portfolio/i }));

    await waitFor(() =>
      expect(screen.getByText(/No broker with id/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/HTTP 404/)).toBeInTheDocument();
  });

  it("shows a real error state on network failure, never a fabricated portfolio", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("network down"),
    );

    render(<PortfolioView />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /view portfolio/i }));

    await waitFor(() =>
      expect(screen.getByText(/DATA_UNAVAILABLE/)).toBeInTheDocument(),
    );
    expect(screen.queryByText("99000.00000000")).not.toBeInTheDocument();
  });
});
