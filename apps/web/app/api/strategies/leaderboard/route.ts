import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /strategies/leaderboard` (Phase 59).
 *
 * Same pass-through shape as every other proxy route in this app (see
 * `app/api/strategies/route.ts` and the backtests-list GET in
 * `app/api/strategies/[strategyId]/versions/[versionId]/backtests/route.ts`):
 * the JWT stays in the httpOnly cookie and is read server-side here, never
 * by the browser, and the backend's status and JSON come back verbatim.
 * Only the backend's own `limit`/`offset`/`min_status` params are
 * forwarded on the GET — nothing here re-shapes or re-sorts the response,
 * including a 200 with an empty `items` array, which is a normal, honest
 * answer and not an error.
 */
export async function GET(request: NextRequest) {
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  const incoming = request.nextUrl.searchParams;
  const query = new URLSearchParams();
  for (const key of ["limit", "offset", "min_status"]) {
    const value = incoming.get(key);
    if (value !== null) query.set(key, value);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";

  let r: Response;
  try {
    r = await fetch(backendUrl(`/strategies/leaderboard${suffix}`), {
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
