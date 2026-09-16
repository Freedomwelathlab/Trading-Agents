import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /backtest-runs` (Phase 71/D089) — the cross-strategy
 * backtest history behind `/strategies/history`.
 *
 * Distinct from `./[runId]/route.ts`, which proxies a single run. Next
 * routes the collection and the member separately, so both files exist for
 * what is one resource on the backend.
 *
 * Query parameters (`symbol`, `status`, `limit`, `offset`) are forwarded
 * verbatim rather than rebuilt here — the backend owns their names and
 * their validation, and a second copy of that in this layer is two places
 * that have to agree.
 */
export async function GET(request: NextRequest) {
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  const query = request.nextUrl.searchParams.toString();
  let r: Response;
  try {
    r = await fetch(backendUrl(`/backtest-runs${query ? `?${query}` : ""}`), {
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
