import { expect, type Page } from "@playwright/test";

/**
 * Blocks until React has hydrated the server-rendered markup.
 *
 * This is not cosmetic. Every interactive surface in this app is a client
 * component whose behaviour (`onSubmit`, `onClick`) only exists after
 * hydration; clicking the login button before then triggers the browser's
 * NATIVE form submission instead, which navigates to `/login?` and silently
 * invalidates the test. React stamps `__reactFiber$…`/`__reactProps$…` keys
 * onto every host node it hydrates, so their presence is a real signal rather
 * than a sleep. `networkidle` is deliberately not used: the dev server holds
 * an HMR socket open, so it may never settle.
 */
export async function waitForHydration(page: Page): Promise<void> {
  await page.waitForFunction(() => {
    const nodes = document.querySelectorAll("main, form, button");
    for (const node of nodes) {
      if (Object.keys(node).some((key) => key.startsWith("__react"))) return true;
    }
    return false;
  });
}

/** `page.goto` followed by {@link waitForHydration}. */
export async function gotoHydrated(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await waitForHydration(page);
}

/**
 * Signs in through the real login form against the real backend
 * (docs/DECISIONS.md D045). Deliberately NOT a shortcut that injects a
 * token: the httpOnly-cookie login flow (D020) is itself part of what this
 * suite exists to prove, and every spec that needs a session should get one
 * the same way a user does.
 */
export async function login(
  page: Page,
  user: { email: string; password: string },
): Promise<void> {
  await gotoHydrated(page, "/login");
  await page.getByLabel("Email").fill(user.email);
  await page.getByLabel("Password").fill(user.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await waitForHydration(page);
}

/**
 * Fills and submits the "Submit paper trade" form with a caller-supplied
 * `estimated_price`. Supplying the price on purpose: it is the backend's
 * caller-authoritative path (D017), so the whole trade is deterministic and
 * no market-data vendor is consulted — which means these specs assert on real
 * risk/portfolio decisions rather than on whatever a live quote happened to be.
 */
export async function submitTrade(
  page: Page,
  trade: {
    brokerId: string;
    symbol: string;
    side?: "buy" | "sell";
    quantity: string;
    estimatedPrice?: string;
    stopPrice?: string;
  },
): Promise<void> {
  await waitForHydration(page);
  const form = page.locator("section", { hasText: "Submit paper trade" }).first();
  await form.getByLabel("Broker ID").fill(trade.brokerId);
  await form.getByLabel("Symbol").fill(trade.symbol);
  await form.getByLabel("Side").selectOption(trade.side ?? "buy");
  await form.getByLabel("Quantity").fill(trade.quantity);
  await form.getByLabel("Est. price (optional)").fill(trade.estimatedPrice ?? "");
  await form.getByLabel("Stop price (optional)").fill(trade.stopPrice ?? "");
  await form.getByRole("button", { name: "Submit trade" }).click();
}
