import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /admin/market-data/backfill` (D070), Phase 82. The
 * backend runs the ingestion synchronously inside the request, so this
 * can take a while for an intraday interval over months; the browser
 * waits and gets the real job row back (status succeeded / partial /
 * failed with the vendor's own error).
 */
export async function POST(request: NextRequest) {
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }
  const body = await request.text();
  let r: Response;
  try {
    r = await fetch(backendUrl("/admin/market-data/backfill"), {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body,
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
