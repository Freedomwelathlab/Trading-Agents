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

  // --- Phase 52: the per-analyst reads the response now actually carries ---

  const TRADE_FIELDS = {
    order_id: "ap-analysts",
    status: "filled",
    approved: true,
    block_reason: null,
    detail: null,
    fill_quantity: "1",
    fill_price: "123.45",
    side: "buy",
    quantity: "1",
    rationale: "clean breakout above resistance",
  };

  it("renders all three analyst reads when the response carries all three", async () => {
    mockOnce({
      ...TRADE_FIELDS,
      technical_analyst: {
        stance: "bullish",
        summary: "Trading above its 20-day average.",
        confidence: "0.7",
        indicator_context: "SMA(20)=118.20, RSI(14)=61.40",
      },
      fundamental_analyst: {
        stance: "neutral",
        summary: "A P/E of 25 with no growth figures reported.",
        confidence: "0.4",
        data_source: "longbridge",
        fundamentals_as_of: "2026-09-01T12:00:00Z",
      },
      news_analyst: {
        stance: "bearish",
        summary: "Three recent headlines, none positive.",
        confidence: "0.6",
        headline_count: 3,
      },
    });
    await submit();

    const technical = screen.getByTestId("technical-analyst");
    expect(technical).toHaveAttribute("data-analyst-state", "read");
    expect(technical).toHaveAttribute("data-stance", "bullish");
    expect(technical).toHaveTextContent("Trading above its 20-day average.");
    expect(technical).toHaveTextContent("0.7");
    // D021's real, deterministically-computed values, shown verbatim.
    expect(technical).toHaveTextContent("SMA(20)=118.20, RSI(14)=61.40");

    const fundamental = screen.getByTestId("fundamental-analyst");
    expect(fundamental).toHaveAttribute("data-analyst-state", "read");
    expect(fundamental).toHaveTextContent(/P\/E of 25/);
    expect(fundamental).toHaveTextContent("longbridge");

    const news = screen.getByTestId("news-analyst");
    expect(news).toHaveAttribute("data-analyst-state", "read");
    expect(news).toHaveAttribute("data-stance", "bearish");
    expect(news).toHaveTextContent("Three recent headlines, none positive.");
    expect(news).toHaveTextContent("3");

    // The obsolete Phase 47 caveat is gone: a breakdown now exists.
    expect(
      screen.queryByTestId("analyst-breakdown-unavailable"),
    ).not.toBeInTheDocument();

    // The real trade fields are still rendered, unchanged.
    expect(screen.getByText(/clean breakout above resistance/)).toBeInTheDocument();
    expect(screen.getByText("buy")).toBeInTheDocument();
  });

  it("says a null analyst produced no read, and invents nothing for it", async () => {
    mockOnce({
      ...TRADE_FIELDS,
      technical_analyst: null,
      fundamental_analyst: null,
      news_analyst: {
        stance: "neutral",
        summary: "Quiet news flow this week.",
        confidence: "0.2",
        headline_count: 2,
      },
    });
    await submit();

    for (const testId of ["technical-analyst", "fundamental-analyst"]) {
      const panel = screen.getByTestId(testId);
      expect(panel).toHaveAttribute("data-analyst-state", "no-read");
      expect(panel).toHaveTextContent(/No read on this request/i);
      // Never a fabricated stance or confidence standing in for absence.
      expect(panel).not.toHaveAttribute("data-stance");
      expect(panel).not.toHaveTextContent(/bullish|bearish|neutral/i);
      expect(panel).not.toHaveTextContent(/confidence/i);
    }

    // Nullability is per analyst: the one that did run is shown in full.
    const news = screen.getByTestId("news-analyst");
    expect(news).toHaveAttribute("data-analyst-state", "read");
    expect(news).toHaveTextContent("Quiet news flow this week.");
  });

  it("renders no analyst section at all for an older-shaped response", async () => {
    // No analyst key of any kind — a body from before Phase 52. Nothing
    // truthful can be said about the analysts, so nothing is said, exactly
    // as PortfolioVerdict treats a response with no portfolio_* fields.
    mockOnce(TRADE_FIELDS);
    await submit();

    expect(screen.queryByTestId("analyst-reads")).not.toBeInTheDocument();
    expect(screen.queryByTestId("technical-analyst")).not.toBeInTheDocument();
    expect(screen.queryByTestId("fundamental-analyst")).not.toBeInTheDocument();
    expect(screen.queryByTestId("news-analyst")).not.toBeInTheDocument();
  });

  it("shows no analyst section before a trade has been proposed", () => {
    render(<AgentTradeForm />);
    expect(screen.queryByTestId("analyst-reads")).not.toBeInTheDocument();
    expect(screen.queryByTestId("technical-analyst")).not.toBeInTheDocument();
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
