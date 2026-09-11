import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /deployments/{deploymentId}/drift-checks?limit=&offset=`
 * (Phase 66, D084) — one append-only `strategy_drift_checks` row per
 * successful runner cycle that completed a drift check, newest first, each
 * saying whether the deployment's actual win rate matches the reference
 * backtest's, or why it couldn't yet be compared. Same pass-through shape
 * as every other proxy route: verbatim status/body. 404/403 (ownership)
 * pass through unchanged.
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

  const limit = request.nextUrl.searchParams.get("limit");
  const offset = request.nextUrl.searchParams.get("offset");
  const qs = new URLSearchParams();
  if (limit) qs.set("limit", limit);
  if (offset) qs.set("offset", offset);
  const query = qs.toString();

  let r: Response;
  try {
    r = await fetch(
      backendUrl(
        `/deployments/${encodeURIComponent(deploymentId)}/drift-checks${query ? `?${query}` : ""}`,
      ),
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
