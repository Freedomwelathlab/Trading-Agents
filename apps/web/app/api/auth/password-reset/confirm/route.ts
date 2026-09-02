import { NextRequest, NextResponse } from "next/server";
import { backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /auth/password-reset/confirm` (docs/DECISIONS.md D062).
 *
 * Unauthenticated by design — the whole point of the flow is that the
 * caller cannot log in — so, like the request handler beside it, there is
 * no cookie read and none set. A successful reset deliberately does NOT
 * issue a session: the user is sent to `/login` to sign in with the
 * password they just chose, which proves the new credential works and
 * keeps "proved you control the reset link" separate from "is now signed
 * in".
 *
 * The backend's `INVALID_OR_EXPIRED_TOKEN` sentinel is forwarded verbatim
 * with its real 400. It is one sentinel for every failure mode on purpose,
 * and this layer must not try to guess a more specific reason for the user.
 */
export async function POST(request: NextRequest) {
  let body: { token?: string; new_password?: string };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: "Invalid request body" }, { status: 400 });
  }

  const { token, new_password } = body;
  if (!token || !new_password) {
    return NextResponse.json(
      { detail: "token and new_password are required" },
      { status: 400 },
    );
  }

  let r: Response;
  try {
    r = await fetch(backendUrl("/auth/password-reset/confirm"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, new_password }),
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
