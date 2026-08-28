/**
 * Server-only helper for talking to the Trading OS API from Next.js route
 * handlers. Never imported from a client component — the backend base URL
 * and the auth cookie both stay server-side.
 */

const DEFAULT_BASE_URL = "http://localhost:8000";

export function backendBaseUrl(): string {
  return process.env.API_BASE_URL ?? DEFAULT_BASE_URL;
}

export const AUTH_COOKIE_NAME = "trading_os_token";

/** Thin wrapper so route handlers don't repeat base-URL joining. */
export function backendUrl(path: string): string {
  return `${backendBaseUrl()}${path}`;
}
