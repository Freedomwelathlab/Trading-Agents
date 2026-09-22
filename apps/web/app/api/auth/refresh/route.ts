import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /auth/refresh` (Phase 84, D100) and rotates the httpOnly
 * cookie to the new token. The browser never sees either token. A 401
 * from the backend (expired, deactivated, or past the session ceiling)
 * comes back as a 401 and the cookie is left alone — a dead session is
 * not resurrected here either.
 */
export async function POST(request: NextRequest) {
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }
  let r: Response;
  try {
    r = await fetch(backendUrl("/auth/refresh"), {
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
  const data = (await r.json().catch(() => null)) as
    | { access_token?: string; expires_at?: string; detail?: string }
    | null;
  if (!r.ok || !data?.access_token) {
    return NextResponse.json(
      { detail: data?.detail ?? "Refresh failed" },
      { status: r.status || 401 },
    );
  }
  const res = NextResponse.json({ ok: true, expires_at: data.expires_at });
  res.cookies.set(AUTH_COOKIE_NAME, data.access_token, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: 60 * 60 * 12,
  });
  return res;
}
