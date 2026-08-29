import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /auth/session` (D031). This is how the UI learns a real
 * expiry for a cookie it is deliberately not allowed to read: the token
 * stays server-side here, and only the backend's expiry metadata is
 * forwarded to the browser — never the token itself.
 */
export async function GET(request: NextRequest) {
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  let r: Response;
  try {
    r = await fetch(backendUrl("/auth/session"), {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
  } catch {
    return NextResponse.json(
      { detail: "DATA_UNAVAILABLE: could not reach the trading API" },
      { status: 503 },
    );
  }

  const data = await r.json().catch(() => null);
  const res = NextResponse.json(data, { status: r.status });
  if (r.status === 401) {
    // The backend just rejected this token — expired, or its user was
    // deactivated. Clear the dead cookie so the next page load is a
    // clean, unauthenticated one rather than another doomed round trip.
    res.cookies.delete(AUTH_COOKIE_NAME);
  }
  return res;
}
