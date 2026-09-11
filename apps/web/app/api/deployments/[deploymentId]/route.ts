import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /deployments/{deploymentId}` (Phase 63, D081) — one
 * strategy deployment's full `StrategyDeploymentResponse`. Same
 * pass-through shape as every other proxy route: verbatim status/body.
 * 404/403 (ownership, re-derived deployment → version → strategy → owner)
 * pass through unchanged for the client to render.
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
      backendUrl(`/deployments/${encodeURIComponent(deploymentId)}`),
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
