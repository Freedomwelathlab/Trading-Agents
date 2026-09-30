import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import OptionsBotPanel from "@/components/options/OptionsBotPanel";

/**
 * The options paper bot panel (Phase 102, D122).
 *
 * What must hold: only PAPER brokers are offered; creating sends exactly
 * the operator's inputs (and no width for a single option); a pending bot
 * shows Approve and nothing else pretends it is running; a backend
 * refusal is shown verbatim rather than as success; and the trades table
 * shows the rows the backend returned, with an open trade labelled open.
 */

const BOT = {
  id: "b1",
  name: "Put spreads",
  broker_id: "p1",
  status: "pending_approval",
  underlying: "TQQQ.US",
  structure_type: "bull_put",
  target_delta: "0.3000",
  dte_min: 3,
  dte_max: 14,
  spread_width: "2.0000",
  profit_target_pct: "50.0000",
  stop_pct: "100.0000",
  max_concurrent_positions: 2,
  capital_per_trade: "500.00000000",
  last_evaluated_at: null,
};

const TRADE = {
  id: "t1",
  structure_type: "bull_put",
  underlying: "TQQQ.US",
  expiry: "2026-10-16",
  quantity: 2,
  entry_delta: "-0.300000",
  entry_net_price: "-0.180000",
  max_loss: "364.00000000",
  opened_at: "2026-10-05T15:00:00Z",
  closed_at: null,
  exit_reason: null,
  realized_pnl: null,
  return_on_risk: null,
};

type Route = { match: (url: string, init?: RequestInit) => boolean; status?: number; body: unknown };

function wire(routes: Route[]) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      const hit = routes.find((r) => r.match(url, init));
      return new Response(JSON.stringify(hit ? hit.body : {}), {
        status: hit?.status ?? 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return calls;
}

const brokers: Route = {
  match: (u) => u.startsWith("/api/brokers"),
  body: {
    brokers: [
      { id: "p1", name: "Paper One", kind: "paper" },
      { id: "l1", name: "Real Money", kind: "live" },
    ],
  },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("OptionsBotPanel", () => {
  it("offers paper brokers only and lists bots with the approval action", async () => {
    wire([brokers, { match: (u) => u === "/api/options-bots", body: { bots: [BOT] } }]);
    render(<OptionsBotPanel underlying="TQQQ.US" />);
    expect(await screen.findByText("Put spreads")).toBeTruthy();
    await screen.findByRole("option", { name: "Paper One" });
    expect(screen.queryByRole("option", { name: "Real Money" })).toBeNull();
    const row = screen.getByText("Put spreads").closest("tr")!;
    expect(within(row).getByText("pending_approval")).toBeTruthy();
    expect(within(row).getByRole("button", { name: "Approve" })).toBeTruthy();
    expect(within(row).queryByRole("button", { name: "Pause" })).toBeNull();
  });

  it("creates with the operator's inputs and no width for a single option", async () => {
    const calls = wire([
      brokers,
      { match: (u, i) => u === "/api/options-bots" && i?.method === "POST", status: 201, body: BOT },
      { match: (u) => u === "/api/options-bots", body: { bots: [] } },
    ]);
    const { container } = render(<OptionsBotPanel underlying="QQQ.US" />);
    await screen.findByRole("option", { name: "Paper One" });
    fireEvent.change(container.querySelector("#ob-structure")!, {
      target: { value: "long_call" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Create bot/ }));
    await screen.findByText(/pending approval\. It trades nothing/);
    const post = calls.find((c) => c.init?.method === "POST")!;
    const sent = JSON.parse(String(post.init!.body));
    expect(sent).toMatchObject({
      broker_id: "p1",
      underlying: "QQQ.US",
      structure_type: "long_call",
      spread_width: null,
      target_delta: "0.30",
      dte_min: 3,
      dte_max: 14,
    });
  });

  it("shows a backend refusal verbatim", async () => {
    wire([
      brokers,
      {
        match: (u, i) => u === "/api/options-bots" && i?.method === "POST",
        status: 400,
        body: { detail: "LIVE_NOT_SUPPORTED: the options bot runs on PAPER brokers only" },
      },
      { match: (u) => u === "/api/options-bots", body: { bots: [] } },
    ]);
    render(<OptionsBotPanel underlying="TQQQ.US" />);
    await screen.findByRole("option", { name: "Paper One" });
    fireEvent.click(screen.getByRole("button", { name: /Create bot/ }));
    expect((await screen.findByRole("alert")).textContent).toContain("LIVE_NOT_SUPPORTED");
  });

  it("shows the selected bot's trades and learning summary", async () => {
    wire([
      brokers,
      { match: (u) => u === "/api/options-bots", body: { bots: [{ ...BOT, status: "active" }] } },
      { match: (u) => u.startsWith("/api/options-bots/b1/trades"), body: { trades: [TRADE] } },
      {
        match: (u) => u.startsWith("/api/options-bots/b1/stats"),
        body: {
          closed_trades: 3,
          total_pnl: "40.00",
          by_structure: [
            { key: "bull_put", trades: 3, win_rate: "0.6667", expectancy: "13.33",
              expectancy_on_risk: "0.036", total_pnl: "40.00" },
          ],
        },
      },
    ]);
    render(<OptionsBotPanel underlying="TQQQ.US" />);
    fireEvent.click(await screen.findByText("Put spreads"));
    await waitFor(() => expect(screen.getByText("-0.180000")).toBeTruthy());
    expect(screen.getByText("open")).toBeTruthy();
    expect(screen.getByTestId("ob-stats-row").textContent).toContain("67%");
  });
});
