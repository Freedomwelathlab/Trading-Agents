import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end suite (docs/DECISIONS.md D045).
 *
 * Unlike the Vitest component suite (`npm test`), nothing here is mocked:
 * every spec drives a real browser against a real `next dev` server, whose
 * route handlers forward to a REAL Trading OS API backed by a REAL Postgres
 * with real seeded rows. That is the entire point of this suite — the Vitest
 * tests already cover rendering against fabricated responses, and repeating
 * that here would prove nothing new.
 *
 * Because of that, this suite CANNOT run on its own: it needs a backend, a
 * migrated database, and the fixtures `scripts/seed_e2e.py` writes. See
 * `docs/DEVELOPMENT_WORKFLOW.md` ("Running the e2e suite") for the full
 * bring-up. It is deliberately not wired into `npm test`.
 *
 * Environment:
 * - `E2E_API_BASE_URL`  backend base URL (default `http://localhost:8000`),
 *                       passed to `next dev` as `API_BASE_URL` so the route
 *                       handlers proxy to whichever backend this run targets.
 * - `E2E_WEB_PORT`      port for the `next dev` server (default 3100).
 * - `E2E_SEED_COMMAND`  command run once before the suite to (re)seed the
 *                       fixtures. Several specs mutate real broker state, so
 *                       a fresh seed per run is what makes them repeatable.
 *                       Set `E2E_SKIP_SEED=1` to skip it when the database
 *                       was seeded by hand immediately beforehand.
 */

const apiBaseUrl = process.env.E2E_API_BASE_URL ?? "http://localhost:8000";
const port = Number(process.env.E2E_WEB_PORT ?? 3100);
// `localhost`, not `127.0.0.1`: Next 16's dev server rejects asset requests
// whose Host is a bare IP that isn't in `allowedDevOrigins`, which returns 403
// for every JS chunk and leaves the page rendered but never hydrated - so a
// click would fire a NATIVE form submit and quietly invalidate the spec.
const baseURL = `http://localhost:${port}`;

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  // Real broker state is mutated by the trade specs, so the suite is
  // single-worker and serial. Parallel workers would race on the same
  // seeded broker rows and produce genuinely different (not flaky-looking
  // but real) risk/portfolio verdicts.
  fullyParallel: false,
  workers: 1,
  // Never retry: a retry here would re-submit real trades against a book
  // the first attempt already changed, so a "pass on retry" would be
  // meaningless. A failure is a failure.
  retries: 0,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? "list" : [["list"]],
  // Generous, on purpose. `next dev` compiles each route and route handler on
  // first request, and a cold compile of a data-fetching component can take
  // tens of seconds - which is a property of the dev server, not a slow
  // assertion. Tight timeouts here would produce flakes that say nothing about
  // the product.
  timeout: 120_000,
  expect: { timeout: 30_000 },
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `npx next dev --port ${port}`,
    url: baseURL,
    // Never reuse a stray dev server: it may be pointed at a different
    // backend than this run targets, which would silently invalidate every
    // assertion below.
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: "pipe",
    stderr: "pipe",
    env: {
      API_BASE_URL: apiBaseUrl,
      NODE_ENV: "development",
    },
  },
});
