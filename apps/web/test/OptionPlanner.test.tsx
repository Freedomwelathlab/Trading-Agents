import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import OptionPlanner from "@/components/options/OptionPlanner";

/**
 * The option planner panel (Phase 91, D110).
 *
 * Two assertions carry the weight. A refusal must render as a RESULT —
 * same place, same weight as a plan — because "score 6 of 12, below the
 * floor" is the answer to the question asked, and showing it as a red
 * failure trains someone to keep adjusting inputs until the red goes
 * away. And the modelled-pricing caveat must be on the plan itself, not
 * in a footnote, because a spread that models at 1.85 and trades at 2.10
 * has lost a third of its edge before the first fill.
 */

const PLAN = {
  symbol: "TQQQ.US",
  planned: true,
  score: 12,
  max_score: 12,
  minimum_tradeable_score: 8,
  grade: "A",
  reason: null,
  path: "reversal",
  dte_band: "short",
  spread: {
    kind: "bull_call",
    long_leg: { right: "call", strike: "80", is_long: true, modeled_price: "1.85" },
    short_leg: { right: "call", strike: "82", is_long: false, modeled_price: "0.95" },
    width: "2",
    net_premium: "0.90",
    is_debit: true,
    max_profit: "110",
    max_loss: "90",
    breakeven: "80.90",
  },
  contracts: 5,
  risk_budget: "500",
  max_loss_total: "450",
  max_profit_total: "550",
  notes: { priced: "MODEL (Black-Scholes) — re-price against real quotes before submitting" },
  pricing_note: "Every premium here is MODELLED. Re-price against option-chain before submitting.",
};

const REFUSAL = {
  ...PLAN,
  planned: false,
  score: 3,
  grade: "C",
  reason: "Signal score 3/12 is below the 8 minimum (§16); grade=C.",
  path: null,
  dte_band: null,
  spread: null,
  contracts: null,
  risk_budget: null,
  max_loss_total: null,
  max_profit_total: null,
};

function wire(body: unknown, status = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        }),
    ),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("OptionPlanner", () => {
  it("renders a plan with its modelled-pricing caveat attached", async () => {
    wire(PLAN);
    render(<OptionPlanner symbol="TQQQ.US" spot="80.10" />);

    fireEvent.click(screen.getByText("Plan"));

    await waitFor(() => expect(screen.getByTestId("plan-spread")).toBeInTheDocument());
    expect(screen.getByText("bull_call")).toBeInTheDocument();
    // The max loss is shown against the budget it had to fit inside.
    expect(screen.getByText(/450/)).toBeInTheDocument();
    expect(screen.getByText(/budget 500/)).toBeInTheDocument();
    expect(screen.getByTestId("pricing-note").textContent).toContain("MODELLED");
  });

  it("renders a refusal as a result, not as an error", async () => {
    wire(REFUSAL);
    render(<OptionPlanner symbol="TQQQ.US" spot="80.10" />);

    fireEvent.click(screen.getByText("Plan"));

    await waitFor(() => expect(screen.getByText("no trade")).toBeInTheDocument());
    expect(screen.getByText(/below the 8 minimum/)).toBeInTheDocument();
    // No spread is invented to fill the space.
    expect(screen.queryByTestId("plan-spread")).toBeNull();
    // The score and the floor it missed are both visible.
    expect(screen.getByText(/score 3\/12 · floor 8/)).toBeInTheDocument();
  });

  it("will not plan without a spot, rather than planning against a guess", async () => {
    wire(PLAN);
    render(<OptionPlanner symbol="TQQQ.US" spot={null} />);
    const button = screen.getByText("Plan") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByText(/no live quote for this symbol/)).toBeInTheDocument();
  });

  it("shows each piece of evidence with the weight it carries", () => {
    wire(PLAN);
    render(<OptionPlanner symbol="TQQQ.US" spot="80.10" />);
    // A sweep is worth two points and a VWAP location one; a form that
    // showed them identically would imply they are the same evidence.
    expect(screen.getByText("Liquidity sweep").parentElement?.textContent).toContain("+2");
    expect(screen.getByText("VWAP location").parentElement?.textContent).toContain("+1");
    expect(screen.getByText(/0 of 12 points/)).toBeInTheDocument();
  });
});
