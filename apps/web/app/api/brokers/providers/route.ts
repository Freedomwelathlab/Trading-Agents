import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /brokers/providers` (Phase 90, D109) — the broker
 * catalogue: what each venue trades and whether this build has an adapter
 * for it. Public facts about public APIs; carries no credential.
 */
export async function GET(request: NextRequest) {
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }
  let r: Response;
  try {
    r = await fetch(backendUrl("/brokers/providers"), {
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
