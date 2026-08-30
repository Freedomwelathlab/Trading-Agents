import { expect, test } from "@playwright/test";
import { ADMIN, AUTH_COOKIE_NAME } from "./fixtures";
import { gotoHydrated, login } from "./helpers";

/**
 * The real login → protected-page flow (docs/DECISIONS.md D045).
 *
 * Every assertion here is against a real response from a real backend: a real
 * bcrypt password check, a real JWT, and the real httpOnly cookie the route
 * handler sets (D020). Nothing is stubbed.
 */
test.describe("authentication", () => {
  test("valid credentials reach the dashboard and set an httpOnly session cookie", async ({
    page,
    context,
  }) => {
    await login(page, ADMIN);

    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

    const cookie = (await context.cookies()).find((c) => c.name === AUTH_COOKIE_NAME);
    expect(cookie, "the login route handler must set the session cookie").toBeTruthy();
    expect(cookie!.httpOnly, "the JWT must not be readable by browser JS (D020)").toBe(true);
    expect(cookie!.value.length).toBeGreaterThan(0);

    // The same guarantee, from the browser's side: document.cookie cannot see it.
    const visibleToJs = await page.evaluate(() => document.cookie);
    expect(visibleToJs).not.toContain(AUTH_COOKIE_NAME);
  });

  test("invalid credentials show the backend's real error and do not redirect", async ({
    page,
    context,
  }) => {
    await gotoHydrated(page, "/login");
    await page.getByLabel("Email").fill(ADMIN.email);
    await page.getByLabel("Password").fill("this-is-not-the-password");
    await page.getByRole("button", { name: "Sign in" }).click();

    // Scoped to the login form: Next's own route announcer is also role="alert".
    const alert = page.locator("form").getByRole("alert");
    await expect(alert).toBeVisible();
    // The backend's own 401 detail, rendered verbatim - not a client-side guess.
    await expect(alert).toHaveText("Incorrect email or password.");

    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByRole("heading", { name: "Dashboard" })).toHaveCount(0);

    const cookie = (await context.cookies()).find((c) => c.name === AUTH_COOKIE_NAME);
    expect(cookie, "a failed login must not set a session cookie").toBeFalsy();
  });

  test("no session at all is bounced off /dashboard by the proxy", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page).toHaveURL(/\/login$/);
  });

  test("an invalid session on a protected page redirects to /login?reason=session-expired", async ({
    page,
    context,
    baseURL,
  }) => {
    // A structurally-present but unverifiable token: the proxy lets it through
    // (it only checks the cookie exists), the backend rejects it with a real
    // 401, and D032's shared handler turns that into an explained redirect
    // rather than a bare "HTTP 401" beside a form that can never work.
    await context.addCookies([
      {
        name: AUTH_COOKIE_NAME,
        value: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.not-a-real-token.signature",
        url: baseURL!,
      },
    ]);

    await page.goto("/dashboard");

    await expect(page).toHaveURL(/\/login\?reason=session-expired$/);
    await expect(page.getByRole("status")).toContainText(
      /session has ended|expired|deactivated/i,
    );
  });
});
