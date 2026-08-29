/**
 * Shared client-side session handling (docs/DECISIONS.md D031).
 *
 * Two jobs:
 *
 * 1. A 401 from any of this app's route handlers means the httpOnly JWT
 *    cookie is missing, expired, or belongs to a user the backend has
 *    since deactivated. Every authenticated component funnels that case
 *    through `handleExpiredSession` so the user lands back on the login
 *    page with a real explanation, instead of reading a bare
 *    "Request failed (HTTP 401)" next to a form that will never work
 *    again.
 *
 * 2. The cookie is httpOnly by design (D020), so client JS cannot read
 *    its expiry. `SESSION_ENDPOINT` is the backend's `GET /auth/session`
 *    proxied server-side — the only truthful source for "how long is
 *    left". Nothing here ever counts down from a locally-remembered
 *    login time; a stale tab, a suspended laptop, or a cookie set in
 *    another tab would all make such a number a fabrication.
 */

export const SESSION_ENDPOINT = "/api/auth/session";

/** Query param the login page reads to explain why the user is back there. */
export const EXPIRED_REASON = "session-expired";

/**
 * Indirection so component tests can assert the redirect without jsdom's
 * unimplemented-navigation noise. Production behaviour is a full
 * document navigation, which is what we want here: it discards all
 * client state tied to a session that no longer exists.
 */
export const sessionNavigation = {
  toLogin(reason: string): void {
    if (typeof window === "undefined") return;
    window.location.assign(`/login?reason=${encodeURIComponent(reason)}`);
  },
};

/**
 * Returns true when `status` means the session is gone (and starts the
 * redirect). Callers use it as an early return before rendering an error:
 *
 *   if (handleExpiredSession(res.status)) return;
 */
export function handleExpiredSession(status: number): boolean {
  if (status !== 401) return false;
  sessionNavigation.toLogin(EXPIRED_REASON);
  return true;
}

export type SessionInfo = {
  user_id: string;
  email: string;
  issued_at: string | null;
  expires_at: string;
  expires_in_seconds: number;
};

/** Below this, the UI warns that the session is about to end. */
export const EXPIRING_SOON_SECONDS = 5 * 60;

export function formatRemaining(seconds: number): string {
  if (seconds <= 0) return "expired";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return `${seconds}s`;
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}
