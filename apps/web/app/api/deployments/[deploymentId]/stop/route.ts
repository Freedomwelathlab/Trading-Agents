import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /deployments/{deploymentId}/stop` (Phase 63, D081) — any
 * non-terminal state → `stopped` (terminal). Deliberately does NOT close
 * open paper positions; unwinding them is a separate act. No request body.
 * A 409 (`ALREADY_STOPPED: …`, plain string `detail`) passes through
 * verbatim.
 */
export async function POST(
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
      backendUrl(`/deployments/${encodeURIComponent(deploymentId)}/stop`),
      {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
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
