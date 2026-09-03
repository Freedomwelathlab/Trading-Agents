import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `DELETE /watchlists/{id}/items/{symbol}` (Phase 50). The symbol
 * is encoded, not normalized — the backend applies the same trim/upper it
 * applied on the way in, so casing is settled in exactly one place.
 */
export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ watchlistId: string; symbol: string }> },
) {
  const { watchlistId, symbol } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  let r: Response;
  try {
    r = await fetch(
      backendUrl(
        `/watchlists/${encodeURIComponent(watchlistId)}/items/${encodeURIComponent(symbol)}`,
      ),
      {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      },
    );
  } catch {
    return NextResponse.json(
      { detail: "DATA_UNAVAILABLE: could not reach the trading API" },
      { status: 503 },
    );
  }

  if (r.status === 204) {
    return new NextResponse(null, { status: 204 });
  }

  const data = await r.json().catch(() => null);
  return NextResponse.json(data, { status: r.status });
}
