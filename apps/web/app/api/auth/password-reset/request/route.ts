import { NextRequest, NextResponse } from "next/server";
import { backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /auth/password-reset/request` (docs/DECISIONS.md D062).
 *
 * Unlike `app/api/auth/login/route.ts`, there is no cookie to set here —
 * this endpoint is unauthenticated and issues no session. The proxy
 * convention is otherwise identical: forward the backend's own status and
 * body untouched, and answer a dead backend with the same
 * `DATA_UNAVAILABLE:` sentinel every other handler in this app uses.
 *
 * Forwarding the body verbatim matters more here than elsewhere. The
 * backend deliberately answers 200 with one fixed message whether or not
 * the address is registered; a handler that "helpfully" rewrote that into
 * "we've emailed you" would reintroduce a claim the backend was careful
 * not to make, and one that is false whenever no email provider is
 * configured.
 */
export async function POST(request: NextRequest) {
  let body: { email?: string };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: "Invalid request body" }, { status: 400 });
  }

  const { email } = body;
  if (!email) {
    return NextResponse.json({ detail: "email is required" }, { status: 400 });
  }

  let r: Response;
  try {
    r = await fetch(backendUrl("/auth/password-reset/request"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
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
