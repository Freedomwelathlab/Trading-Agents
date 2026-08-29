import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import TradeForm from "@/components/TradeForm";

describe("TradeForm", () => {
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

  it("renders a rejected trade's block_reason legibly", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        order_id: "abc123",
        status: "rejected",
        approved: false,
        block_reason: "POSITION_LIMIT_EXCEEDED",
        detail: null,
        fill_quantity: null,
        fill_price: null,
      }),
    });

    render(<TradeForm />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /submit trade/i }));

    await waitFor(() =>
      expect(screen.getByText("POSITION_LIMIT_EXCEEDED")).toBeInTheDocument(),
    );
    expect(screen.getByText("rejected")).toBeInTheDocument();
  });

  it("renders a filled trade's fill price/quantity", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        order_id: "abc456",
        status: "filled",
        approved: true,
        block_reason: null,
        detail: null,
        fill_quantity: "10",
        fill_price: "100",
      }),
    });

    render(<TradeForm />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /submit trade/i }));

    await waitFor(() => expect(screen.getByText("filled")).toBeInTheDocument());
    expect(screen.getByText("10")).toBeInTheDocument();
    expect(screen.getByText("100")).toBeInTheDocument();
  });

  // --- D038: Portfolio Manager verdict rendering (closes the D029 gap) ---

  function mockOnce(body: Record<string, unknown>) {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => body,
    });
  }

  async function submit() {
    render(<TradeForm />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /submit trade/i }));
    await waitFor(() =>
      expect(screen.getByTestId("risk-verdict")).toBeInTheDocument(),
    );
  }

  it("adds no Portfolio Manager panel when the action is 'approve'", async () => {
    mockOnce({
      order_id: "p-approve",
      status: "filled",
      approved: true,
      block_reason: null,
      detail: null,
      portfolio_action: "approve",
      portfolio_binding_constraint: null,
      portfolio_detail: "No portfolio-level constraint is breached.",
      portfolio_requested_quantity: "10",
      fill_quantity: "10",
      fill_price: "100",
    });
    await submit();

    expect(screen.queryByTestId("portfolio-verdict")).not.toBeInTheDocument();
    expect(screen.getByText("filled")).toBeInTheDocument();
  });

  it("renders a 'modify' as a resize, with both quantities and the constraint", async () => {
    mockOnce({
      order_id: "p-modify",
      status: "filled",
      approved: true,
      block_reason: null,
      detail: null,
      portfolio_action: "modify",
      portfolio_binding_constraint: "symbol_concentration",
      portfolio_detail:
        "SYMBOL_CONCENTRATION would reach 40.00% of equity (limit 25.00%); quantity reduced.",
      portfolio_requested_quantity: "100",
      fill_quantity: "62",
      fill_price: "100",
    });
    await submit();

    const panel = screen.getByTestId("portfolio-verdict");
    expect(panel).toHaveAttribute("data-portfolio-action", "modify");
    expect(panel).toHaveTextContent(/Resized by the Portfolio Manager/i);
    expect(screen.getByTestId("portfolio-requested-quantity")).toHaveTextContent(
      "100",
    );
    expect(screen.getByTestId("portfolio-filled-quantity")).toHaveTextContent(
      "62",
    );
    expect(panel).toHaveTextContent("symbol_concentration");
    expect(panel).toHaveTextContent(/quantity reduced/);
  });

  it("renders a portfolio 'reject' distinctly from a Risk Engine rejection", async () => {
    mockOnce({
      order_id: "p-reject",
      status: "rejected",
      approved: true,
      block_reason: null,
      detail: null,
      portfolio_action: "reject",
      portfolio_binding_constraint: "cash_reserve",
      portfolio_detail: "CASH_RESERVE would fall to 1.00% of equity (min 5.00%).",
      portfolio_requested_quantity: "50",
      fill_quantity: null,
      fill_price: null,
    });
    await submit();

    const panel = screen.getByTestId("portfolio-verdict");
    expect(panel).toHaveAttribute("data-portfolio-action", "reject");
    expect(panel).toHaveTextContent(/Rejected by the Portfolio Manager/i);
    expect(panel).toHaveTextContent(/not the Risk Engine/i);
    expect(panel).toHaveTextContent("cash_reserve");

    // Visually distinct from a risk rejection: violet panel, and the risk
    // block is NOT painted the amber a risk rejection uses.
    expect(panel.className).toMatch(/violet/);
    expect(panel.className).not.toMatch(/amber/);
    expect(screen.getByTestId("risk-verdict").className).not.toMatch(/amber/);
  });

  it("shows nothing about approval when portfolio_action is null", async () => {
    mockOnce({
      order_id: "p-null",
      status: "rejected",
      approved: false,
      block_reason: "POSITION_LIMIT_EXCEEDED",
      detail: null,
      portfolio_action: null,
      portfolio_binding_constraint: null,
      portfolio_detail: null,
      portfolio_requested_quantity: null,
      fill_quantity: null,
      fill_price: null,
    });
    await submit();

    // Never ran => rendered as absent, never as an approval.
    expect(screen.queryByTestId("portfolio-verdict")).not.toBeInTheDocument();
    expect(screen.queryByText(/Portfolio Manager/i)).not.toBeInTheDocument();
    // The risk rejection is still the one shown, in its own amber block.
    expect(screen.getByText("POSITION_LIMIT_EXCEEDED")).toBeInTheDocument();
    expect(screen.getByTestId("risk-verdict").className).toMatch(/amber/);
  });

  it("fabricates no verdict for an older-shaped response with no portfolio fields", async () => {
    mockOnce({
      order_id: "p-legacy",
      status: "filled",
      approved: true,
      block_reason: null,
      detail: null,
      fill_quantity: "10",
      fill_price: "100",
    });
    await submit();

    expect(screen.queryByTestId("portfolio-verdict")).not.toBeInTheDocument();
    expect(screen.queryByText(/Portfolio Manager/i)).not.toBeInTheDocument();
  });

  it("surfaces a real 403 error detail, not a generic message", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 403,
      json: async () => ({ detail: "No access grant for broker abc" }),
    });

    render(<TradeForm />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /submit trade/i }));

    await waitFor(() =>
      expect(screen.getByText(/No access grant for broker abc/)).toBeInTheDocument(),
    );
  });
});
