import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /brokers/{id}/orders/{orderId}/cancel` (Phase 84, D101).
 * Pass-through: the backend's 409s (not cancellable), 400 (no live
 * adapter) and 502 (venue error) come back verbatim.
 */
export async function POST(
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
        `/brokers/${encodeURIComponent(brokerId)}/orders/${encodeURIComponent(orderId)}/cancel`,
      ),
      { method: "POST", headers: { Authorization: `Bearer ${token}` }, cache: "no-store" },
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
