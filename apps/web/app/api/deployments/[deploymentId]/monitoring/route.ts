import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /deployments/{deploymentId}/monitoring` (Phase 65, D083) —
 * the "actual vs expected" performance snapshot for one deployment: its own
 * closed round trips and open positions versus the reference backtest for
 * its strategy version, if one exists. Same pass-through shape as every
 * other proxy route: verbatim status/body. 404/403 (ownership) pass through
 * unchanged.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ deploymentId: string }> },
) {
  const { deploymentId } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  let r: Response;
  try {
    r = await fetch(
      backendUrl(`/deployments/${encodeURIComponent(deploymentId)}/monitoring`),
      {
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
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
