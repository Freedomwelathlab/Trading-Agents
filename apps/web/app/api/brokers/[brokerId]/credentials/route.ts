import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies one broker's credentials (Phase 90, D109).
 *
 * GET returns presence per field and the values of non-secret fields
 * only; PUT writes the whole set; DELETE forgets it. The secret travels
 * one way — this proxy passes the body straight through and never logs
 * it, and the backend never returns it in any shape.
 */
async function forward(
  request: NextRequest,
  brokerId: string,
  init: RequestInit,
): Promise<NextResponse> {
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }
  let r: Response;
  try {
    r = await fetch(backendUrl(`/brokers/${encodeURIComponent(brokerId)}/credentials`), {
      ...init,
      headers: {
        ...(init.headers ?? {}),
        Authorization: `Bearer ${token}`,
      },
      cache: "no-store",
    });
  } catch {
    return NextResponse.json(
      { detail: "DATA_UNAVAILABLE: could not reach the trading API" },
      { status: 503 },
    );
  }
  if (r.status === 204) return new NextResponse(null, { status: 204 });
  const data = await r.json().catch(() => null);
  return NextResponse.json(data, { status: r.status });
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ brokerId: string }> },
) {
  const { brokerId } = await params;
  return forward(request, brokerId, { method: "GET" });
}

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ brokerId: string }> },
) {
  const { brokerId } = await params;
  const body = await request.text();
  return forward(request, brokerId, {
    method: "PUT",
    body,
    headers: { "Content-Type": "application/json" },
  });
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ brokerId: string }> },
) {
  const { brokerId } = await params;
  return forward(request, brokerId, { method: "DELETE" });
}
