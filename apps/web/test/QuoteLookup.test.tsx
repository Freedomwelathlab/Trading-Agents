import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import QuoteLookup from "@/components/QuoteLookup";

describe("QuoteLookup", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders a real quote on success", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        symbol: "AAPL.US",
        price: "150.25",
        as_of: "2026-08-27T00:00:00Z",
        source: "longbridge",
      }),
    });

    render(<QuoteLookup />);
    fireEvent.click(screen.getByRole("button", { name: /get quote/i }));

    await waitFor(() => expect(screen.getByText("150.25")).toBeInTheDocument());
    expect(screen.getByText("longbridge")).toBeInTheDocument();
  });

  it("surfaces the real NOT_CONFIGURED sentinel on a 503, not a generic error", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 503,
      json: async () => ({
        detail: "NOT_CONFIGURED: no market data vendor is wired up",
      }),
    });

    render(<QuoteLookup />);
    fireEvent.click(screen.getByRole("button", { name: /get quote/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/NOT_CONFIGURED: no market data vendor is wired up/),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText(/HTTP 503/)).toBeInTheDocument();
  });

  it("surfaces the real NO_DATA_AVAILABLE sentinel on a 404", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: async () => ({
        detail: "NO_DATA_AVAILABLE: vendor has no data for ZZZZ.US",
      }),
    });

    render(<QuoteLookup />);
    fireEvent.click(screen.getByRole("button", { name: /get quote/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/NO_DATA_AVAILABLE: vendor has no data for ZZZZ.US/),
      ).toBeInTheDocument(),
    );
  });

  it("shows a real error state on network failure, never fabricated data", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("network down"),
    );

    render(<QuoteLookup />);
    fireEvent.click(screen.getByRole("button", { name: /get quote/i }));

    await waitFor(() =>
      expect(screen.getByText(/DATA_UNAVAILABLE/)).toBeInTheDocument(),
    );
    expect(screen.queryByText("150.25")).not.toBeInTheDocument();
  });
});
