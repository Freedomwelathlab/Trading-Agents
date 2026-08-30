import { expect, test } from "@playwright/test";
import {
  CONCENTRATED_BROKER_ID,
  CONCENTRATED_BROKER_NAME,
  FLAT_BROKER_ID,
  FLAT_BROKER_NAME,
  TRADER,
} from "./fixtures";
import { login, waitForHydration } from "./helpers";

/**
 * `BrokerDiscovery` against the real `GET /brokers` (D034), which lists only
 * the brokers the calling user actually holds a `BrokerGrant` for. Every row
 * asserted below is a real row the seed script wrote (docs/DECISIONS.md D045).
 */
test.describe("broker discovery", () => {
  test("lists the user's real granted brokers and fills one into the trade form", async ({
    page,
  }) => {
    await login(page, TRADER);

    await waitForHydration(page);
    const discovery = page.locator("section", { hasText: "My brokers" }).first();
    await expect(discovery.getByText(FLAT_BROKER_NAME)).toBeVisible();
    await expect(discovery.getByText(CONCENTRATED_BROKER_NAME)).toBeVisible();
    await expect(discovery.getByText(FLAT_BROKER_ID)).toBeVisible();

    const tradeForm = page.locator("section", { hasText: "Submit paper trade" }).first();
    // The field starts empty: nothing is ever pre-filled with a guessed or
    // sample broker id.
    await expect(tradeForm.getByLabel("Broker ID")).toHaveValue("");

    const row = discovery.locator("tr", { hasText: CONCENTRATED_BROKER_ID });
    await row.getByRole("button", { name: "Use" }).click();

    await expect(row.getByRole("button", { name: "In use" })).toBeVisible();
    await expect(tradeForm.getByLabel("Broker ID")).toHaveValue(CONCENTRATED_BROKER_ID);

    // Same real id reaches the other broker-scoped forms (D034).
    const agentForm = page.locator("section", { hasText: "Agent trade (AI proposal)" }).first();
    await expect(agentForm.getByLabel("Broker ID")).toHaveValue(CONCENTRATED_BROKER_ID);
  });
});
