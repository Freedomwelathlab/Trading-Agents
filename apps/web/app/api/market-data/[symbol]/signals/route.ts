import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /market-data/{symbol}/signals` (Phase 85, D102) — the B/S
 * markers the chart draws, with each one's score and evidence. Pure
 * pass-through: a 404 (no stored bars) and a 400 (unknown setup) come
 * back as themselves.
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
  const path = `/market-data/${encodeURIComponent(symbol)}/signals${query ? `?${query}` : ""}`;

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
