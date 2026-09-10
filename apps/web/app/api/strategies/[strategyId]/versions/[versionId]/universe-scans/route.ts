import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /strategies/{strategyId}/versions/{versionId}/universe-scans`
 * (run one validated strategy across many symbols and rank them) and
 * `GET .../universe-scans?limit=&offset=` (list scan summaries, newest
 * first) — Phase 60.
 *
 * Same pass-through shape as every other proxy route: verbatim
 * status/body, no logic of its own. A 409 (`VERSION_NOT_VALIDATED: ...`,
 * a plain string `detail`), a 422 (`symbols: []`, 51+ symbols, or nothing
 * ingested — also a plain string `detail`) and a 201 whose body reports a
 * `"succeeded"` scan with `num_succeeded: 0` all pass through unchanged
 * for the client to render.
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
        `/strategies/${encodeURIComponent(strategyId)}/versions/${encodeURIComponent(versionId)}/universe-scans`,
      ),
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

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ strategyId: string; versionId: string }> },
) {
  const { strategyId, versionId } = await params;
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
        `/strategies/${encodeURIComponent(strategyId)}/versions/${encodeURIComponent(versionId)}/universe-scans${query ? `?${query}` : ""}`,
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
