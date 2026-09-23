import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import OptionChainTerminal from "@/components/options/OptionChainTerminal";

/**
 * The option chain's honesty rules (Phase 89, D108).
 *
 * Three things are asserted here and each is a place where a table can
 * quietly say something untrue: a contract the vendor did not quote must
 * read as a dash rather than as a price, the at-the-money row must be
 * marked from a real live quote or not at all, and a chain where nothing
 * came back must say so rather than render a screen of dashes and leave
 * the reader to work it out.
 */

const EXPIRY = "2026-10-16";

const CHAIN = {
  symbol: "TQQQ.US",
  expiry: EXPIRY,
  as_of: "2026-09-23T14:00:00Z",
  source: "longbridge",
  quoted_contracts: 1,
  note: "Strikes come from the vendor's own contract ladder.",
  calls: [
    {
      contract_symbol: "C80",
      strike: "80",
      right: "call",
      last_price: "3.10",
      bid: "3.00",
      ask: "3.20",
      mid: "3.10",
      spread: "0.20",
      volume: 120,
      open_interest: 0,
      implied_vol: "0.42",
      delta: "0.55",
      gamma: null,
      theta: null,
      vega: null,
    },
    {
      contract_symbol: "C85",
      strike: "85",
      right: "call",
      last_price: null,
      bid: null,
      ask: null,
      mid: null,
      spread: null,
      volume: null,
      open_interest: null,
      implied_vol: null,
      delta: null,
      gamma: null,
      theta: null,
      vega: null,
    },
  ],
  puts: [],
};

function wire(
  handlers: Record<string, { status?: number; body: unknown }>,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const key = Object.keys(handlers).find((k) => url.includes(k));
      if (!key) return new Response(JSON.stringify({ detail: "no stub" }), { status: 404 });
      const { status = 200, body } = handlers[key];
      return new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

beforeEach(() => {
  vi.useRealTimers();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("OptionChainTerminal", () => {
  it("renders a dash for every field the vendor did not quote", async () => {
    wire({
      "option-expiries": { body: { symbol: "TQQQ.US", expiries: [EXPIRY], source: "lb" } },
      "option-chain": { body: CHAIN },
      "/api/quote/": { status: 404, body: { detail: "DATA_UNAVAILABLE" } },
    });

    render(<OptionChainTerminal initialSymbol="TQQQ.US" />);

    // The quoted call shows its real bid.
    await waitFor(() => expect(screen.getByText("3.00")).toBeInTheDocument());
    // The unquoted one contributes dashes, and nowhere is there a 0.00
    // standing in for a price nobody made.
    const zeros = screen.queryAllByText("0.00");
    expect(zeros).toHaveLength(0);
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
    // Zero open interest is a measurement and survives as 0, unlike a
    // missing price.
    expect(screen.getAllByText("0").length).toBeGreaterThan(0);
  });

  it("marks no row at the money when there is no live quote to mark it from", async () => {
    wire({
      "option-expiries": { body: { symbol: "TQQQ.US", expiries: [EXPIRY], source: "lb" } },
      "option-chain": { body: CHAIN },
      "/api/quote/": { status: 404, body: { detail: "DATA_UNAVAILABLE" } },
    });

    render(<OptionChainTerminal initialSymbol="TQQQ.US" />);

    await waitFor(() => expect(screen.getByText("3.00")).toBeInTheDocument());
    expect(screen.queryByTestId("atm-row")).toBeNull();
    expect(screen.getByText(/No live quote for TQQQ.US/)).toBeInTheDocument();
  });

  it("marks the nearest strike once a live quote exists", async () => {
    wire({
      "option-expiries": { body: { symbol: "TQQQ.US", expiries: [EXPIRY], source: "lb" } },
      "option-chain": { body: CHAIN },
      "/api/quote/": {
        body: { symbol: "TQQQ.US", price: "84.10", as_of: "2026-09-23T14:00:00Z", source: "lb" },
      },
    });

    render(<OptionChainTerminal initialSymbol="TQQQ.US" />);

    await waitFor(() => expect(screen.getByTestId("atm-row")).toBeInTheDocument());
    // 84.10 is nearer 85 than 80.
    expect(screen.getByTestId("atm-row").textContent).toContain("85.00");
  });

  it("says outright when no contract in the expiry came back with a market", async () => {
    wire({
      "option-expiries": { body: { symbol: "TQQQ.US", expiries: [EXPIRY], source: "lb" } },
      "option-chain": { body: { ...CHAIN, quoted_contracts: 0 } },
      "/api/quote/": { status: 404, body: { detail: "DATA_UNAVAILABLE" } },
    });

    render(<OptionChainTerminal initialSymbol="TQQQ.US" />);

    await waitFor(() =>
      expect(screen.getByText(/came back without a market/)).toBeInTheDocument(),
    );
  });

  it("reports a vendor that lists no options as the vendor's own answer", async () => {
    wire({
      "option-expiries": { body: { symbol: "AAPL.US", expiries: [], source: "longbridge" } },
      "/api/quote/": { status: 404, body: { detail: "DATA_UNAVAILABLE" } },
    });

    render(<OptionChainTerminal initialSymbol="AAPL.US" />);

    await waitFor(() =>
      expect(screen.getByText(/lists no option contracts/)).toBeInTheDocument(),
    );
  });
});
