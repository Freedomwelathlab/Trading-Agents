import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `PATCH /admin/brokers/{broker_id}/mode` (Phase 43 / D058, first
 * frontend in Phase 47).
 *
 * Same pass-through contract as the sibling admin proxies. The two
 * refusals this endpoint is most likely to return — the 400
 * `LIVE_KIND_CONFIRMATION_REQUIRED` and the 409 for a broker that already
 * has recorded orders or a simulated book — are forwarded with their
 * status and `detail` intact so the UI can render what the backend
 * actually said. Neither is re-worded or softened here: both describe an
 * audit-integrity rule the operator needs stated precisely.
 *
 * As with `POST /admin/brokers`, `confirm_live` is never injected or
 * defaulted by this layer; the body arrives and leaves as the client sent
 * it.
 */
export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ brokerId: string }> },
) {
  const { brokerId } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: "Invalid request body" }, { status: 400 });
  }

  let r: Response;
  try {
    r = await fetch(
      backendUrl(`/admin/brokers/${encodeURIComponent(brokerId)}/mode`),
      {
        method: "PATCH",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      },
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
