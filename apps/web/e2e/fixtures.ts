/**
 * The rows `scripts/seed_e2e.py` writes into the real database, mirrored here
 * so a spec can address a specific broker without first scraping its id out of
 * the UI (docs/DECISIONS.md D045). Keep the two files in sync; the seed script
 * is the source of truth.
 *
 * These are throwaway paper-only test credentials for a local test database.
 * They are not, and must never become, credentials for anything real.
 */

export const ADMIN = {
  email: "e2e-admin@example.test",
  password: "e2e-admin-password-2026",
} as const;

export const TRADER = {
  email: "e2e-trader@example.test",
  password: "e2e-trader-password-2026",
} as const;

/**
 * A paper broker seeded with 100,000 cash and no positions.
 *
 * Every UI trade against this broker must use {@link FLAT_SYMBOL}: the trade
 * form has no "marks" input, and the backend prices a held symbol only from
 * the marks the caller supplies plus the traded symbol's own estimated price.
 * Trading a second symbol here after the first fill would fail with a real
 * DATA_UNAVAILABLE for the unpriced holding — a true property of the product,
 * not a test artifact.
 */
export const FLAT_BROKER_ID = "3e2e0000-0000-4000-8000-000000000001";
export const FLAT_BROKER_NAME = "E2E Flat Paper";
export const FLAT_SYMBOL = "MSFT.US";

/**
 * A paper broker seeded with 80,000 cash and 200 shares of
 * {@link CONCENTRATED_SYMBOL}, sized so a 100-share buy at 100.00 clears the
 * Risk Engine and is then shrunk to 50 by the trade-path Portfolio Manager's
 * 25%-of-equity per-symbol cap (D029/D038).
 */
export const CONCENTRATED_BROKER_ID = "3e2e0000-0000-4000-8000-000000000002";
export const CONCENTRATED_BROKER_NAME = "E2E Concentrated Paper";
export const CONCENTRATED_SYMBOL = "AAPL.US";

/**
 * A real, active paper broker that neither fixture user holds a grant for.
 * Used to assert the backend's real 403 — an authorization failure, distinct
 * from the 404 a non-existent broker id would produce.
 */
export const UNGRANTED_BROKER_ID = "3e2e0000-0000-4000-8000-000000000003";

/** Name of the httpOnly session cookie set by `app/api/auth/login/route.ts`. */
export const AUTH_COOKIE_NAME = "trading_os_token";
