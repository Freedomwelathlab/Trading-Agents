import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /market-data/{symbol}/depth` (Phase 74, D092) — the live
 * order book behind the Markets terminal's depth ladder.
 *
 * Pass-through, verbatim status and body, like every other proxy here.
 * That matters more than usual for this endpoint: its three statuses are
 * three different facts, and collapsing them would destroy the
 * distinction the backend exists to preserve.
 *
 *   503 — no market-data vendor is wired at all.
 *   404 — a vendor answered and had no priced level. Ordinary for a
 *         closed market, and also what an entitlement without a depth
 *         ladder looks like; the two are not separable from one response.
 *   200 — a real book.
 *
 * A 404 here must never be rendered as an empty-but-real book of zeros.
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
  const path = `/market-data/${encodeURIComponent(symbol)}/depth${query ? `?${query}` : ""}`;

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
