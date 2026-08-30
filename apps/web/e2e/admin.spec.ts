import { expect, test } from "@playwright/test";
import {
  ADMIN,
  CONCENTRATED_BROKER_ID,
  FLAT_BROKER_ID,
  TRADER,
} from "./fixtures";
import { gotoHydrated, login } from "./helpers";

/**
 * `/admin` against the real admin listing endpoints (D031), which are gated by
 * the backend's real `admin:manage` permission — never by anything the
 * frontend guesses (docs/DECISIONS.md D023/D045).
 */
test.describe("admin page", () => {
  test("an admin sees the real users, roles and broker grants", async ({ page }) => {
    await login(page, ADMIN);
    await page.getByRole("link", { name: "Admin" }).click();
    await expect(page).toHaveURL(/\/admin$/);

    const users = page.locator("section", { hasText: "Refresh users" }).first();
    await expect(users.getByText(ADMIN.email)).toBeVisible();
    await expect(users.getByText(TRADER.email)).toBeVisible();
    // A listing must never carry password material (D031).
    await expect(users).not.toContainText(ADMIN.password);
    await expect(users).not.toContainText("$2b$");

    const roles = page.locator("section", { hasText: "Refresh roles" }).first();
    await expect(roles.getByText("e2e-admin", { exact: true })).toBeVisible();
    await expect(roles.getByText("e2e-trader", { exact: true })).toBeVisible();
    await expect(roles).toContainText("admin:manage");
    await expect(roles).toContainText("trade:submit:paper");

    const grants = page.locator("section", { hasText: "Refresh grants" }).first();
    await expect(grants.getByText(FLAT_BROKER_ID).first()).toBeVisible();
    await expect(grants.getByText(CONCENTRATED_BROKER_ID).first()).toBeVisible();
  });

  test("a non-admin sees the backend's real 403 on every listing, not a hidden page", async ({
    page,
  }) => {
    await login(page, TRADER);
    // The page is deliberately reachable by any authenticated user: the real
    // gate is the backend's 403, rendered honestly (D023).
    await gotoHydrated(page, "/admin");
    await expect(page.getByRole("heading", { name: "Admin" })).toBeVisible();

    // Scoped to <main>: Next's own route announcer is a body-level role="alert".
    const alerts = page.locator("main").getByRole("alert");
    await expect(alerts.first()).toBeVisible();
    await expect(alerts.first()).toContainText("HTTP 403");
    // Users, roles and grants listings all fail the same way.
    await expect(alerts).toHaveCount(3);
    await expect(page.getByText(ADMIN.email)).toHaveCount(0);
  });
});
