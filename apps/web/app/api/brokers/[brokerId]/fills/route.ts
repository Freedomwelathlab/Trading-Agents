import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /brokers/{broker_id}/fills` (Phase 48 / D065).
 *
 * This is **not** the orders listing with rejections filtered out — it is
 * a different grain: one row per actual execution, carrying its parent
 * order's `symbol`/`side` and the joined `broker_kind`. A partially-filled
 * order would contribute several rows here and exactly one to the orders
 * listing. The distinction is the whole reason the backend exposes two
 * endpoints, so this proxy keeps them two.
 *
 * Same shape as the sibling orders proxy: awaited `params`, a 401 before
 * any network call when the auth cookie is absent, `limit`/`offset`
 * allowlisted rather than forwarded blindly, and a real
 * `503 DATA_UNAVAILABLE` when the API cannot be reached.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ brokerId: string }> },
) {
  const { brokerId } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  const incoming = request.nextUrl.searchParams;
  const query = new URLSearchParams();
  for (const key of ["limit", "offset"]) {
    const value = incoming.get(key);
    if (value !== null) query.set(key, value);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";

  let r: Response;
  try {
    r = await fetch(backendUrl(`/brokers/${encodeURIComponent(brokerId)}/fills${suffix}`), {
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
