import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /brokers/{broker_id}/orders` (Phase 48 / D065) for the
 * Phase 51 trade-history panel.
 *
 * A pure pass-through: it adds the httpOnly auth cookie as a bearer token,
 * forwards only the two documented query params, and returns the backend's
 * own status and body unchanged. It never filters rows, never re-labels a
 * status, and never substitutes a body of its own for a backend error —
 * the orders listing is the audit trail (spec §17), and a proxy that
 * quietly reshaped it would be the one place a reader could not trust.
 *
 * Same convention as `app/api/portfolio/[brokerId]/history/route.ts`:
 * `params` is awaited, an absent cookie is a 401 before any network call,
 * and an unreachable API is a real `503 DATA_UNAVAILABLE` rather than a
 * fabricated empty page.
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
    r = await fetch(backendUrl(`/brokers/${encodeURIComponent(brokerId)}/orders${suffix}`), {
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
