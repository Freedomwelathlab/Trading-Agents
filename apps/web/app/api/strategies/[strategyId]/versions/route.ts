import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /strategies/{strategyId}/versions` (Phase 54) — forks a new
 * draft version, defaulting to the latest version when the body omits
 * `from_version_id`. Same pass-through shape as every other proxy route in
 * this app: verbatim status/body, no logic of its own.
 */
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ strategyId: string }> },
) {
  const { strategyId } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  let body: unknown = {};
  try {
    const text = await request.text();
    body = text ? JSON.parse(text) : {};
  } catch {
    return NextResponse.json({ detail: "Invalid request body" }, { status: 400 });
  }

  let r: Response;
  try {
    r = await fetch(
      backendUrl(`/strategies/${encodeURIComponent(strategyId)}/versions`),
      {
        method: "POST",
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
