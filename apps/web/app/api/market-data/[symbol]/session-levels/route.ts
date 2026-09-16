import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /market-data/{symbol}/session-levels` (Phase 74, D092) —
 * PDH/PDL/PDC, premarket extremes, the opening range and session VWAP,
 * derived from this platform's own stored bars.
 *
 * `bar_interval` is forwarded verbatim rather than rebuilt here, so an
 * unknown value gets the backend's own 422 rather than a second,
 * differently-worded refusal.
 *
 * Every level in the response is nullable and a null means the stored
 * bars do not support that level. It is never a zero.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ symbol: string }> },
) {
  const { symbol } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  const query = request.nextUrl.searchParams.toString();
  const path = `/market-data/${encodeURIComponent(symbol)}/session-levels${query ? `?${query}` : ""}`;

  let r: Response;
  try {
    r = await fetch(backendUrl(path), {
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
