import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /strategies/{strategyId}/versions/{versionId}` and
 * `PATCH /strategies/{strategyId}/versions/{versionId}` (Phase 54). The
 * PATCH is a full replace of `definition`; the backend returns 409 (a plain
 * string `detail`) when the version is no longer a draft, which comes back
 * unchanged for the client to render as a normal error.
 */
export async function GET(
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
        `/strategies/${encodeURIComponent(strategyId)}/versions/${encodeURIComponent(versionId)}`,
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

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ strategyId: string; versionId: string }> },
) {
  const { strategyId, versionId } = await params;
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
      backendUrl(
        `/strategies/${encodeURIComponent(strategyId)}/versions/${encodeURIComponent(versionId)}`,
      ),
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
