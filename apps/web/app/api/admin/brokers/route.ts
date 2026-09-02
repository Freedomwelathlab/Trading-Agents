import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /admin/brokers` (Phase 43 / D058, first frontend in
 * Phase 47).
 *
 * A pass-through in the exact shape every other admin proxy in this
 * directory uses: attach the httpOnly cookie as a bearer token, forward
 * the body untouched, return the backend's status and JSON verbatim.
 *
 * It deliberately does NOT inspect, default, or add `confirm_live`. That
 * flag is the caller's explicit statement of intent to designate a
 * real-money broker, and a proxy that supplied it would turn a
 * server-enforced safeguard into one this layer could silently satisfy on
 * the user's behalf. If the client omits it for a `kind: "live"` body the
 * backend's own 400 (`LIVE_KIND_CONFIRMATION_REQUIRED`) comes back here
 * unchanged, which is the correct outcome.
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
    r = await fetch(backendUrl("/admin/brokers"), {
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
