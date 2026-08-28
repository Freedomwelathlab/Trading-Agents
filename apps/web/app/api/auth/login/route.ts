import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies the login form to the backend's OAuth2 password flow and, on
 * success, stores the JWT in an httpOnly cookie so client-side JS never
 * touches it. See docs/DECISIONS.md D020 for the storage tradeoff.
 */
export async function POST(request: NextRequest) {
  let body: { email?: string; password?: string };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: "Invalid request body" }, { status: 400 });
  }

  const { email, password } = body;
  if (!email || !password) {
    return NextResponse.json(
      { detail: "email and password are required" },
      { status: 400 },
    );
  }

  const form = new URLSearchParams();
  form.set("username", email);
  form.set("password", password);

  let backendResponse: Response;
  try {
    backendResponse = await fetch(backendUrl("/auth/login"), {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form.toString(),
    });
  } catch {
    return NextResponse.json(
      { detail: "DATA_UNAVAILABLE: could not reach the trading API" },
      { status: 503 },
    );
  }

  const data = await backendResponse.json().catch(() => null);

  if (!backendResponse.ok || !data?.access_token) {
    return NextResponse.json(
      data ?? { detail: "Login failed" },
      { status: backendResponse.status || 401 },
    );
  }

  const res = NextResponse.json({ ok: true });
  res.cookies.set(AUTH_COOKIE_NAME, data.access_token, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    // The backend JWT carries its own expiry; this is just a cap so a
    // stale cookie doesn't linger forever if the browser tab is never closed.
    maxAge: 60 * 60 * 8,
  });
  return res;
}
