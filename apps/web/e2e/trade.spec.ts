import { expect, test } from "@playwright/test";
import {
  CONCENTRATED_BROKER_ID,
  CONCENTRATED_SYMBOL,
  FLAT_BROKER_ID,
  FLAT_SYMBOL,
  TRADER,
  UNGRANTED_BROKER_ID,
} from "./fixtures";
import { login, submitTrade } from "./helpers";

/**
 * Real trade submissions through `TradeForm` (docs/DECISIONS.md D045).
 *
 * Each of these writes a real `orders` row (and, when filled, a real `fills`
 * row) in a real Postgres, produced by the real deterministic Risk Engine
 * (D004) and the real trade-path Portfolio Manager (D029). The rendered
 * verdicts asserted below are the backend's own; no fetch is intercepted and
 * no response body is fabricated.
 *
 * Serial by necessity: these mutate the same real broker book, and the
 * Portfolio Manager MODIFY case depends on the concentrated broker still
 * holding exactly its seeded position.
 */
test.describe.configure({ mode: "serial" });

test.describe("trade submission", () => {
  test("an approved trade renders a real risk verdict and a real fill", async ({ page }) => {
    await login(page, TRADER);

    // 50 x 100.00 = 5,000 notional on 100,000 equity: inside the 10%
    // single-position cap, and a 1.00 stop distance risks 50 against the
    // 1,000 per-trade risk budget.
    await submitTrade(page, {
      brokerId: FLAT_BROKER_ID,
      symbol: FLAT_SYMBOL,
      quantity: "50",
      estimatedPrice: "100.00",
      stopPrice: "99.00",
    });

    const verdict = page.getByTestId("risk-verdict");
    await expect(verdict).toBeVisible();
    await expect(verdict).toContainText("filled");
    await expect(verdict).toContainText("true"); // approved (Risk Engine)
    // A real fill at the caller-authoritative price, and a real order id.
    await expect(verdict).toContainText("100.00");
    await expect(verdict).toContainText(
      /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/,
    );

    // D038's contract: a Portfolio Manager APPROVE adds no panel at all.
    // Absence here is the assertion, not an oversight.
    await expect(page.getByTestId("portfolio-verdict")).toHaveCount(0);
  });

  test("a trade the Risk Engine blocks renders the real block reason, not a fill", async ({
    page,
  }) => {
    await login(page, TRADER);

    // No stop price: the account's real limits require one (D004).
    await submitTrade(page, {
      brokerId: FLAT_BROKER_ID,
      symbol: FLAT_SYMBOL,
      quantity: "11",
      estimatedPrice: "100.00",
    });

    const verdict = page.getByTestId("risk-verdict");
    await expect(verdict).toBeVisible();
    await expect(verdict).toContainText("rejected");
    await expect(verdict).toContainText("missing_stop_price");
    await expect(verdict).toContainText(
      "This account's risk limits require a stop price on every proposal.",
    );
    // Amber = the Risk Engine said no (D038 keys the tone on `approved`).
    await expect(verdict).toHaveClass(/amber/);

    // The Portfolio Manager never ran, so it must render as absent — never
    // as an approval (D029/D038).
    await expect(page.getByTestId("portfolio-verdict")).toHaveCount(0);
  });

  test("the Portfolio Manager's real MODIFY verdict is rendered beside the risk verdict", async ({
    page,
  }) => {
    await login(page, TRADER);

    // The concentrated broker holds 200 AAPL.US (20,000) against 100,000
    // equity. A 100-share buy at 100.00 clears the Risk Engine (10,000 ==
    // the 10% single-position cap exactly) and is then shrunk by the
    // Portfolio Manager's 25%-of-equity per-symbol cap, which leaves only
    // 5,000 of headroom — a real resize to 50 shares.
    await submitTrade(page, {
      brokerId: CONCENTRATED_BROKER_ID,
      symbol: CONCENTRATED_SYMBOL,
      quantity: "100",
      estimatedPrice: "100.00",
      stopPrice: "99.00",
    });

    const risk = page.getByTestId("risk-verdict");
    await expect(risk).toBeVisible();
    // The Risk Engine approved; only the portfolio gate resized it.
    await expect(risk).toContainText("true");
    await expect(risk).not.toHaveClass(/amber/);

    const portfolio = page.getByTestId("portfolio-verdict");
    await expect(portfolio).toBeVisible();
    await expect(portfolio).toHaveAttribute("data-portfolio-action", "modify");
    await expect(portfolio).toContainText("Resized by the Portfolio Manager");
    await expect(portfolio).toContainText("symbol_concentration");
    await expect(page.getByTestId("portfolio-requested-quantity")).toHaveText("100");
    await expect(page.getByTestId("portfolio-filled-quantity")).toHaveText("50");
  });

  test("a broker the user holds no grant for returns the backend's real 403", async ({ page }) => {
    await login(page, TRADER);

    await submitTrade(page, {
      // A real, active broker row that this user holds no `broker_grants`
      // row for. The backend, not the frontend, is the authorization
      // boundary - and a 403 here (rather than a 404) is what proves the
      // broker exists and access was refused.
      brokerId: UNGRANTED_BROKER_ID,
      symbol: FLAT_SYMBOL,
      quantity: "1",
      estimatedPrice: "100.00",
      stopPrice: "99.00",
    });

    // Scoped to the form: Next's own route announcer is also role="alert".
    const form = page.locator("section", { hasText: "Submit paper trade" }).first();
    const alert = form.getByRole("alert");
    await expect(alert).toBeVisible();
    await expect(alert).toContainText("HTTP 403");
    await expect(page.getByTestId("risk-verdict")).toHaveCount(0);
  });
});
