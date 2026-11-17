import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /admin/alerts/test` (Phase 111, D138). The backend sends
 * one fixed message to the configured alert channel and answers 200 only
 * when the channel accepted it; 503 NOT_CONFIGURED and 502
 * ALERT_NOT_DELIVERED are passed through unchanged.
 */
export async function POST(request: NextRequest) {
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }
  let r: Response;
  try {
    r = await fetch(backendUrl("/admin/alerts/test"), {
      method: "POST",
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
