import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /deployments/{deploymentId}/signals?limit=&offset=` (Phase
 * 63, D081) — every Phase-61 `SignalEvaluation` any of this deployment's
 * runs produced, newest first: the deployment's own signal trail. Same
 * pass-through shape as every other proxy route: verbatim status/body.
 * 404/403 (ownership) pass through unchanged.
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
        `/deployments/${encodeURIComponent(deploymentId)}/signals${query ? `?${query}` : ""}`,
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
