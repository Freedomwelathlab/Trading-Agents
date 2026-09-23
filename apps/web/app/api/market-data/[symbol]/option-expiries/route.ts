import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /market-data/{symbol}/option-expiries` (Phase 89, D108).
 *
 * Pure pass-through, including the status. A 503 (no vendor wired) and a
 * 404 (a vendor that answered and listed nothing) mean different things on
 * an option chain and must not be flattened into one empty table here.
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
  const path = `/market-data/${encodeURIComponent(symbol)}/option-expiries${query ? `?${query}` : ""}`;

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
