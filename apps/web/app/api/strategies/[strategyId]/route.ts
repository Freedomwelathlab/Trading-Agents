import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /strategies/{strategyId}` and `PATCH /strategies/{strategyId}`
 * (Phase 54). Owner-only server-side: this handler forwards the caller's own
 * token and never inspects who owns what — the backend's 403 for someone
 * else's strategy and 404 for one that does not exist both come back
 * unchanged.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ strategyId: string }> },
) {
  const { strategyId } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  let r: Response;
  try {
    r = await fetch(backendUrl(`/strategies/${encodeURIComponent(strategyId)}`), {
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

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ strategyId: string }> },
) {
  const { strategyId } = await params;
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
    r = await fetch(backendUrl(`/strategies/${encodeURIComponent(strategyId)}`), {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
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
