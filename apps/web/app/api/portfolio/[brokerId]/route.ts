import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendGetWithBody } from "@/lib/backend";

/**
 * Proxies GET /brokers/{broker_id}/portfolio (D022). The backend expects
 * `marks` as a JSON body on a GET request, so this can't use the plain
 * `fetch`-based pattern the other route handlers use (Node's fetch/undici
 * rejects a body on GET/HEAD) — see lib/backend.ts's `backendGetWithBody`
 * and docs/DECISIONS.md D026 for why.
 */
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ brokerId: string }> },
) {
  // Exposed to the browser as POST (this route handler's own method is not
  // the backend's — the backend call it makes is still a real GET). A
  // browser-originated GET request cannot carry a JSON body either, so the
  // client component posts to this same-origin route handler, which then
  // issues the actual GET-with-body to the backend server-side.
  const { brokerId } = await params;
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

  let result: { status: number; text: string };
  try {
    result = await backendGetWithBody(
      `/brokers/${encodeURIComponent(brokerId)}/portfolio`,
      { Authorization: `Bearer ${token}` },
      JSON.stringify(body ?? {}),
    );
  } catch {
    return NextResponse.json(
      { detail: "DATA_UNAVAILABLE: could not reach the trading API" },
      { status: 503 },
    );
  }

  let data: unknown = null;
  try {
    data = JSON.parse(result.text);
  } catch {
    data = null;
  }
  return NextResponse.json(data, { status: result.status });
}
