import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies every `/autotrade/*` route (Phase 81, D098) — the bot surface is
 * a dozen small endpoints with one shape each, so one pass-through handler
 * rather than a file per route. Same contract as the other proxies: the
 * JWT stays in the httpOnly cookie, the backend's status and JSON come
 * back verbatim, and only `limit`/`offset` are forwarded on a GET.
 *
 * Only `autotrade/...` paths can be reached through here — the segment is
 * fixed by the folder, so this is not a general proxy.
 */

async function forward(
  request: NextRequest,
  params: Promise<{ path: string[] }>,
  method: "GET" | "POST",
) {
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }
  const { path } = await params;
  const safe = path.map((p) => encodeURIComponent(p)).join("/");

  const incoming = request.nextUrl.searchParams;
  const query = new URLSearchParams();
  for (const key of ["limit", "offset"]) {
    const value = incoming.get(key);
    if (value !== null) query.set(key, value);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";

  const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
  let body: string | undefined;
  if (method === "POST") {
    const text = await request.text();
    if (text) {
      headers["Content-Type"] = "application/json";
      body = text;
    }
  }

  let r: Response;
  try {
    r = await fetch(backendUrl(`/autotrade/${safe}${suffix}`), {
      method,
      headers,
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

export async function GET(
  request: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
) {
  return forward(request, ctx.params, "GET");
}

export async function POST(
  request: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
) {
  return forward(request, ctx.params, "POST");
}
