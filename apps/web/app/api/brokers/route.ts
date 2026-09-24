import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /brokers` (D034) — the brokers the authenticated caller
 * actually holds a grant for. Same route-handler pattern as every other
 * authenticated call in this app: the JWT stays in the httpOnly cookie
 * and is read server-side here, never by the browser. Only the backend's
 * own `limit`/`offset`/`approved_only` params are forwarded.
 */
export async function GET(request: NextRequest) {
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  const incoming = request.nextUrl.searchParams;
  const query = new URLSearchParams();
  for (const key of ["limit", "offset", "approved_only"]) {
    const value = incoming.get(key);
    if (value !== null) query.set(key, value);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";

  let r: Response;
  try {
    r = await fetch(backendUrl(`/brokers${suffix}`), {
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
  return NextResponse.json(data, { status: r.status });
}
