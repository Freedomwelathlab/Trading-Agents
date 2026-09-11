import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /deployments/{deploymentId}/approve` (Phase 63, D081) — the
 * mandatory human gate: `pending_approval` → `active`. This is the ONE
 * action gated by the separate, stricter `strategy:approve_deployment`
 * permission, so a caller holding only `strategy:deploy` gets a 403 here —
 * it passes through verbatim for the client to explain. A 409
 * (`NOT_PENDING_APPROVAL: …`, plain string `detail`) also passes through
 * unchanged. Body: optional `{note?: string}`.
 */
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ deploymentId: string }> },
) {
  const { deploymentId } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));

  let r: Response;
  try {
    r = await fetch(
      backendUrl(`/deployments/${encodeURIComponent(deploymentId)}/approve`),
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body ?? {}),
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
