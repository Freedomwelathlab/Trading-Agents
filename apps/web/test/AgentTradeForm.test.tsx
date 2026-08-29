import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import AgentTradeForm from "@/components/AgentTradeForm";

describe("AgentTradeForm", () => {
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

  it("renders a filled agent trade's side/quantity/rationale", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        order_id: "abc123",
        status: "filled",
        approved: true,
        block_reason: null,
        detail: null,
        fill_quantity: "1",
        fill_price: "123.45",
        side: "buy",
        quantity: "1",
        rationale: "clean breakout above resistance",
      }),
    });

    render(<AgentTradeForm />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /propose agent trade/i }));

    await waitFor(() =>
      expect(screen.getByText(/clean breakout above resistance/)).toBeInTheDocument(),
    );
    expect(screen.getByText("buy")).toBeInTheDocument();
    expect(screen.getByText("123.45")).toBeInTheDocument();
  });

  it("renders a rejected agent trade's block_reason", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        order_id: "abc124",
        status: "rejected",
        approved: false,
        block_reason: "POSITION_LIMIT_EXCEEDED",
        detail: null,
        fill_quantity: null,
        fill_price: null,
        side: "buy",
        quantity: "50",
        rationale: "momentum continuation",
      }),
    });

    render(<AgentTradeForm />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /propose agent trade/i }));

    await waitFor(() =>
      expect(screen.getByText("POSITION_LIMIT_EXCEEDED")).toBeInTheDocument(),
    );
    expect(screen.getByText("rejected")).toBeInTheDocument();
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
    render(<AgentTradeForm />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /propose agent trade/i }));
    await waitFor(() =>
      expect(screen.getByTestId("risk-verdict")).toBeInTheDocument(),
    );
  }

  it("adds no Portfolio Manager panel when the action is 'approve'", async () => {
    mockOnce({
      order_id: "ap-approve",
      status: "filled",
      approved: true,
      block_reason: null,
      detail: null,
      portfolio_action: "approve",
      portfolio_binding_constraint: null,
      portfolio_detail: "No portfolio-level constraint is breached.",
      portfolio_requested_quantity: "1",
      fill_quantity: "1",
      fill_price: "123.45",
      side: "buy",
      quantity: "1",
      rationale: "clean breakout above resistance",
    });
    await submit();

    expect(screen.queryByTestId("portfolio-verdict")).not.toBeInTheDocument();
    expect(screen.getByText("filled")).toBeInTheDocument();
  });

  it("renders a 'modify' as a resize, with both quantities and the constraint", async () => {
    mockOnce({
      order_id: "ap-modify",
      status: "filled",
      approved: true,
      block_reason: null,
      detail: null,
      portfolio_action: "modify",
      portfolio_binding_constraint: "symbol_concentration",
      portfolio_detail:
        "SYMBOL_CONCENTRATION would reach 40.00% of equity (limit 25.00%); quantity reduced.",
      portfolio_requested_quantity: "80",
      fill_quantity: "31",
      fill_price: "123.45",
      side: "buy",
      quantity: "80",
      rationale: "momentum continuation",
    });
    await submit();

    const panel = screen.getByTestId("portfolio-verdict");
    expect(panel).toHaveAttribute("data-portfolio-action", "modify");
    expect(panel).toHaveTextContent(/Resized by the Portfolio Manager/i);
    expect(screen.getByTestId("portfolio-requested-quantity")).toHaveTextContent(
      "80",
    );
    expect(screen.getByTestId("portfolio-filled-quantity")).toHaveTextContent(
      "31",
    );
    expect(panel).toHaveTextContent("symbol_concentration");
    expect(panel).toHaveTextContent(/quantity reduced/);
  });

  it("renders a portfolio 'reject' distinctly from a Risk Engine rejection", async () => {
    mockOnce({
      order_id: "ap-reject",
      status: "rejected",
      approved: true,
      block_reason: null,
      detail: null,
      portfolio_action: "reject",
      portfolio_binding_constraint: "max_open_positions",
      portfolio_detail: "MAX_OPEN_POSITIONS would reach 21 (limit 20).",
      portfolio_requested_quantity: "5",
      fill_quantity: null,
      fill_price: null,
      side: "buy",
      quantity: "5",
      rationale: "diversifying add",
    });
    await submit();

    const panel = screen.getByTestId("portfolio-verdict");
    expect(panel).toHaveAttribute("data-portfolio-action", "reject");
    expect(panel).toHaveTextContent(/Rejected by the Portfolio Manager/i);
    expect(panel).toHaveTextContent(/not the Risk Engine/i);
    expect(panel).toHaveTextContent("max_open_positions");

    expect(panel.className).toMatch(/violet/);
    expect(panel.className).not.toMatch(/amber/);
    expect(screen.getByTestId("risk-verdict").className).not.toMatch(/amber/);
  });

  it("shows nothing about approval when portfolio_action is null", async () => {
    mockOnce({
      order_id: "ap-null",
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
      side: "buy",
      quantity: "50",
      rationale: "momentum continuation",
    });
    await submit();

    expect(screen.queryByTestId("portfolio-verdict")).not.toBeInTheDocument();
    expect(screen.queryByText(/Portfolio Manager/i)).not.toBeInTheDocument();
    expect(screen.getByText("POSITION_LIMIT_EXCEEDED")).toBeInTheDocument();
    expect(screen.getByTestId("risk-verdict").className).toMatch(/amber/);
  });

  it("fabricates no verdict for an older-shaped response with no portfolio fields", async () => {
    mockOnce({
      order_id: "ap-legacy",
      status: "filled",
      approved: true,
      block_reason: null,
      detail: null,
      fill_quantity: "1",
      fill_price: "123.45",
      side: "buy",
      quantity: "1",
      rationale: "clean breakout",
    });
    await submit();

    expect(screen.queryByTestId("portfolio-verdict")).not.toBeInTheDocument();
    expect(screen.queryByText(/Portfolio Manager/i)).not.toBeInTheDocument();
  });

  it("surfaces the real NOT_CONFIGURED sentinel, not a fabricated trade", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 400,
      json: async () => ({
        detail: "NOT_CONFIGURED: no LLM provider is wired up",
      }),
    });

    render(<AgentTradeForm />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /propose agent trade/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/NOT_CONFIGURED: no LLM provider is wired up/),
      ).toBeInTheDocument(),
    );
  });

  it("surfaces the real AGENT_OUTPUT_INVALID sentinel on a 502", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 502,
      json: async () => ({
        detail: "AGENT_OUTPUT_INVALID: provider response did not match the expected schema",
      }),
    });

    render(<AgentTradeForm />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /propose agent trade/i }));

    await waitFor(() =>
      expect(screen.getByText(/AGENT_OUTPUT_INVALID:/)).toBeInTheDocument(),
    );
  });

  it("shows a real error state on network failure, never a fabricated trade", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("network down"),
    );

    render(<AgentTradeForm />);
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: /propose agent trade/i }));

    await waitFor(() =>
      expect(screen.getByText(/DATA_UNAVAILABLE/)).toBeInTheDocument(),
    );
    expect(screen.queryByText("123.45")).not.toBeInTheDocument();
  });
});
