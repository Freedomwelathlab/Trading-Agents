import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { BotCard } from "@/components/library/LibraryBots";
import type { LibraryBot, LibraryBotRun } from "@/components/library/types";

/**
 * A library bot card (Phase 109, D136) shows only what the API returned -
 * a bot nobody has run yet has no equity and no return, not zeros - and
 * offers only the actions its state allows.
 */

const BOT: LibraryBot = {
  id: "b1",
  name: "SPY trend",
  broker_id: "p1",
  status: "active",
  strategy_id: "trend_200d",
  symbols: ["SPY.US"],
  params: { sma_days: 200 },
  capital: "5000.00000000",
  max_drawdown_pct: "20.0000",
  cash: "474.60000000",
  positions: { "SPY.US": "6.00000000" },
  target_weights: { "SPY.US": 1 },
  target_explain: { "SPY.US": "close 754.4 is above its 200-day average 690.1" },
  target_refused: {},
  data_as_of: "2026-11-13",
  last_decision_date: "2026-10-30",
  last_equity: "5012.40000000",
  peak_equity: "5012.40000000",
  return_pct: "0.2480",
  approved_at: "2026-11-16T08:00:00Z",
  paused_reason: null,
  stopped_at: null,
  last_evaluated_at: "2026-11-16T15:00:00Z",
  created_at: "2026-11-16T07:00:00Z",
};

const RUN: LibraryBotRun = {
  id: "r1",
  status: "succeeded",
  started_at: "2026-11-16T15:00:00Z",
  completed_at: "2026-11-16T15:00:02Z",
  orders_filled: 1,
  orders_rejected: 0,
  equity: "5000.00000000",
  decision_date: "2026-10-30",
  target: { "SPY.US": 1 },
  orders: [
    {
      symbol: "SPY.US", side: "buy", quantity: "6", price: "754.23", quote_as_of: "2026-11-16T14:59:30Z",
      quote_source: "yahoo-delayed", outcome: "filled", detail: null, order_id: "o1",
    },
  ],
  detail: "1 filled, 0 rejected, 0 skipped (strategy decision 2026-10-30, data to 2026-11-13)",
};

describe("BotCard", () => {
  it("shows holdings, the rule's reason, the return and the cycle's order", () => {
    render(<BotCard bot={BOT} runs={[RUN]} strategyName="200-day trend filter" busy={false} onAction={() => {}} />);
    expect(screen.getByText("6 SPY.US")).toBeTruthy();
    expect(screen.getByText(/above its 200-day average/)).toBeTruthy();
    expect(screen.getByTestId("library-bot-return").textContent).toBe("+0.25%");
    expect(screen.getByText(/-20% from peak/)).toBeTruthy();
    expect(screen.getByText(/filled · buy 6 SPY.US @ 754.23 \(yahoo-delayed\)/)).toBeTruthy();
  });

  it("shows a dash, not a zero, before the bot has ever been valued", () => {
    const fresh: LibraryBot = {
      ...BOT, status: "pending_approval", positions: {}, target_weights: null, target_explain: {},
      last_equity: null, peak_equity: null, return_pct: null, last_decision_date: null, approved_at: null,
    };
    render(<BotCard bot={fresh} runs={[]} strategyName="200-day trend filter" busy={false} onAction={() => {}} />);
    expect(screen.getByTestId("library-bot-return").textContent).toBe("—");
    expect(screen.getByText(/Not evaluated yet/)).toBeTruthy();
    expect(screen.getByText(/Nothing - all cash/)).toBeTruthy();
  });

  it("offers only the actions the state allows", () => {
    const onAction = vi.fn();
    const { rerender } = render(<BotCard bot={BOT} runs={[]} strategyName="x" busy={false} onAction={onAction} />);
    expect(screen.queryByText("Approve")).toBeNull();
    expect(screen.queryByText("Delete")).toBeNull();
    fireEvent.click(screen.getByText("Run now"));
    expect(onAction).toHaveBeenCalledWith("run");

    rerender(<BotCard bot={{ ...BOT, status: "pending_approval" }} runs={[]} strategyName="x" busy={false} onAction={onAction} />);
    expect(screen.getByText("Approve")).toBeTruthy();
    expect(screen.queryByText("Run now")).toBeNull();

    rerender(
      <BotCard
        bot={{ ...BOT, status: "paused", paused_reason: "Kill switch: drawdown limit of 20% reached on 2026-11-16." }}
        runs={[]} strategyName="x" busy={false} onAction={onAction}
      />,
    );
    expect(screen.getByText("Resume")).toBeTruthy();
    expect(screen.getByText(/Kill switch: drawdown limit/)).toBeTruthy();

    rerender(<BotCard bot={{ ...BOT, status: "stopped" }} runs={[]} strategyName="x" busy={false} onAction={onAction} />);
    expect(screen.getByText("Delete")).toBeTruthy();
    expect(screen.queryByText("Stop")).toBeNull();
  });

  it("says why a symbol was not bought", () => {
    render(
      <BotCard
        bot={{ ...BOT, target_refused: { "QQQ.US": "no room under the position cap or the bot's cash" } }}
        runs={[]} strategyName="x" busy={false} onAction={() => {}}
      />,
    );
    expect(screen.getByText(/QQQ.US not bought: no room under the position cap/)).toBeTruthy();
  });
});
