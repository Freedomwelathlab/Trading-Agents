import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `DELETE /watchlists/{id}` (Phase 50). Owner-only server-side:
 * this handler forwards the caller's own token and never inspects who owns
 * what — the backend's 403 for someone else's watchlist and 404 for one
 * that does not exist both come back unchanged.
 */
export async function DELETE(
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
    r = await fetch(backendUrl(`/watchlists/${encodeURIComponent(watchlistId)}`), {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    });
  } catch {
    return NextResponse.json(
      { detail: "DATA_UNAVAILABLE: could not reach the trading API" },
      { status: 503 },
    );
  }

  // 204 No Content has no body — a Response must not be constructed with
  // both a 204 status and a JSON body.
  if (r.status === 204) {
    return new NextResponse(null, { status: 204 });
  }

  const data = await r.json().catch(() => null);
  return NextResponse.json(data, { status: r.status });
}
