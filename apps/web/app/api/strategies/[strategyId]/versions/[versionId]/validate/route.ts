import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /strategies/{strategyId}/versions/{versionId}/validate`
 * (Phase 54) — no request body. 200 returns the version with
 * `status: "validated"`; 422 carries `{"detail": {"errors": string[]}}`;
 * 409 (not a draft) carries a plain-string `detail`. All three come back
 * verbatim for the client to render.
 */
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ strategyId: string; versionId: string }> },
) {
  const { strategyId, versionId } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  let r: Response;
  try {
    r = await fetch(
      backendUrl(
        `/strategies/${encodeURIComponent(strategyId)}/versions/${encodeURIComponent(versionId)}/validate`,
      ),
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
