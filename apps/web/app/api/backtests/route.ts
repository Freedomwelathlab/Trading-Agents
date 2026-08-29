import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /backtests` (D025, extended by D035's Portfolio Manager
 * counters). Same route-handler shape as every other authenticated
 * feature here: the JWT stays in the httpOnly cookie, is read server-side
 * only, and is never handed to client JS (D020).
 *
 * Unlike the trade routes there is no `brokerId` path segment — the
 * backend endpoint is deliberately not broker-scoped, because a backtest
 * builds its own disposable in-memory paper broker and can never touch a
 * real broker's persisted state (D025). Nothing is added, defaulted, or
 * reinterpreted here: the body goes to the backend as the caller typed
 * it, and whatever status/detail comes back is returned verbatim so the
 * real sentinel (`NOT_CONFIGURED:`, `UNSUPPORTED_DATE_RANGE:`,
 * `DATA_UNAVAILABLE:`) reaches the UI unchanged.
 */
export async function POST(request: NextRequest) {
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
    r = await fetch(backendUrl("/backtests"), {
      method: "POST",
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
