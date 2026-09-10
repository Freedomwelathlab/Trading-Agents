import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /signal-evaluations/{evaluationId}` (Phase 61) — one
 * persisted signal evaluation's full shape, including its plain-language
 * `explanation`. Same pass-through shape as every other proxy route:
 * verbatim status/body. 404/403 (ownership) pass through unchanged for the
 * client to render.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ evaluationId: string }> },
) {
  const { evaluationId } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  let r: Response;
  try {
    r = await fetch(
      backendUrl(`/signal-evaluations/${encodeURIComponent(evaluationId)}`),
      {
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
      },
    );
  } catch {
    return NextResponse.json(
      { detail: "DATA_UNAVAILABLE: could not reach the trading API" },
      { status: 503 },
    );
  }

  const data = await r.json().catch(() => null);
  return NextResponse.json(data, { status: r.status });
}
