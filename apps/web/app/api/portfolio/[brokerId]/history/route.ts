import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /brokers/{broker_id}/portfolio/history` (D027). Unlike the
 * sibling portfolio route, this one takes no body — `limit`/`offset` are
 * plain query params — so it can use ordinary `fetch` and stay a real GET
 * end to end.
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

  // Forward only the params the backend documents; anything else is
  // dropped rather than passed through blindly.
  const incoming = request.nextUrl.searchParams;
  const query = new URLSearchParams();
  for (const key of ["limit", "offset"]) {
    const value = incoming.get(key);
    if (value !== null) query.set(key, value);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";

  let r: Response;
  try {
    r = await fetch(
      backendUrl(`/brokers/${encodeURIComponent(brokerId)}/portfolio/history${suffix}`),
      { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" },
    );
  } catch {
    return NextResponse.json(
      { detail: "DATA_UNAVAILABLE: could not reach the trading API" },
      { status: 503 },
    );
  }

  const data = await r.json().catch(() => null);
  return NextResponse.json(data, { status: r.status });
}
