import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies the read-only connection probe (Phase 94, D113).
 *
 * POST because the probe reaches a third party and is not cacheable, not
 * because it changes anything here: the backend calls the adapter's
 * `get_account_state` and nothing else, so no order is placed and no
 * position is touched.
 *
 * A failed probe is a 200 with `reachable: false` — the venue's refusal is
 * the finding. This proxy therefore passes the backend's status straight
 * through rather than reinterpreting one, and only invents a status when
 * it could not reach the backend at all.
 */
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ brokerId: string }> },
) {
  const { brokerId } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  let r: Response;
  try {
    r = await fetch(backendUrl(`/brokers/${encodeURIComponent(brokerId)}/connection-check`), {
      method: "POST",
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
