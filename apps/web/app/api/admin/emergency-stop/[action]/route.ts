import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies the emergency-stop routes (D039), Phase 82:
 *   GET  /api/admin/emergency-stop/status      -> GET  /admin/emergency-stop
 *   POST /api/admin/emergency-stop/activate    -> POST /admin/emergency-stop
 *   POST /api/admin/emergency-stop/deactivate  -> POST /admin/emergency-stop/deactivate
 * The backend requires a non-blank `reason` on both flips and this layer
 * forwards the body verbatim, so a blank reason is the backend's 422.
 */
function target(action: string): string | null {
  if (action === "status") return "/admin/emergency-stop";
  if (action === "activate") return "/admin/emergency-stop";
  if (action === "deactivate") return "/admin/emergency-stop/deactivate";
  return null;
}

async function forward(request: NextRequest, action: string, method: "GET" | "POST") {
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }
  const path = target(action);
  if (path === null || (method === "GET") !== (action === "status")) {
    return NextResponse.json({ detail: "Unknown emergency-stop action" }, { status: 404 });
  }
  const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
  let body: string | undefined;
  if (method === "POST") {
    headers["Content-Type"] = "application/json";
    body = await request.text();
  }
  let r: Response;
  try {
    r = await fetch(backendUrl(path), { method, headers, body, cache: "no-store" });
  } catch {
    return NextResponse.json(
      { detail: "DATA_UNAVAILABLE: could not reach the trading API" },
      { status: 503 },
    );
  }
  const data = await r.json().catch(() => null);
  return NextResponse.json(data, { status: r.status });
}

export async function GET(request: NextRequest, ctx: { params: Promise<{ action: string }> }) {
  const { action } = await ctx.params;
  return forward(request, action, "GET");
}

export async function POST(request: NextRequest, ctx: { params: Promise<{ action: string }> }) {
  const { action } = await ctx.params;
  return forward(request, action, "POST");
}
