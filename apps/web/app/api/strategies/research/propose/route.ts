import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /strategies/research/propose` (Phase 67 / D085).
 *
 * The same pass-through shape as the POST half of `app/api/strategies/route.ts`:
 * the JWT stays in the httpOnly cookie and is read server-side here, never
 * by the browser, and the backend's status and JSON come back verbatim —
 * including a 400 `NOT_CONFIGURED:` (no LLM provider wired), a 502 (the
 * LLM's own response was unusable), and a 200 whose `is_valid` is `false`
 * (a structurally invalid draft is still a successful call). Nothing here
 * re-shapes, retries, or fabricates a draft.
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
    r = await fetch(backendUrl("/strategies/research/propose"), {
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
