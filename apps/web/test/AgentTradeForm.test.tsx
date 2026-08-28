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
