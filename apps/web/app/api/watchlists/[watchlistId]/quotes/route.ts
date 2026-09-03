import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /watchlists/{id}/quotes` (Phase 50) — the research payoff:
 * every symbol on the list with its live quote.
 *
 * A pure pass-through, deliberately. The backend's per-symbol
 * `unavailable` sentinels arrive here as ordinary rows and are forwarded
 * as-is; this layer never fills one in, drops one, or substitutes a price
 * for one. `cache: "no-store"` for the same reason every other quote path
 * in this app uses it — a cached quote is a stale quote presented as a
 * current one.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ watchlistId: string }> },
) {
  const { watchlistId } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  let r: Response;
  try {
    r = await fetch(backendUrl(`/watchlists/${encodeURIComponent(watchlistId)}/quotes`), {
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
