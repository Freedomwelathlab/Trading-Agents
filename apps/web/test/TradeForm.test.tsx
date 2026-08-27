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
