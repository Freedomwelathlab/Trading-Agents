import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /brokers/{broker_id}/orders/{order_id}` (Phase 48 / D065).
 *
 * The detail route takes no query params at all, so none are forwarded —
 * the allowlist here is empty rather than absent, which is the same rule
 * the sibling listing proxies apply, not a different one.
 *
 * The backend deliberately answers **404** both for an order id that does
 * not exist and for a real one belonging to a broker the caller is not
 * addressing, so that the route cannot be used as an existence oracle
 * (D065). That status is passed through untouched; this proxy must not
 * "helpfully" distinguish the two cases, because it could not do so
 * without defeating the reason they were made identical.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ brokerId: string; orderId: string }> },
) {
  const { brokerId, orderId } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  let r: Response;
  try {
    r = await fetch(
      backendUrl(
        `/brokers/${encodeURIComponent(brokerId)}/orders/${encodeURIComponent(orderId)}`,
      ),
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
