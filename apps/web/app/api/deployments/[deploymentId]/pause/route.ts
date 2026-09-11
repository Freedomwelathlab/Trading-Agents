import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /deployments/{deploymentId}/pause` (Phase 63, D081) —
 * `active` → `paused`; the runner then skips the deployment (writing a
 * `skipped_not_active` run row) until it is resumed. A 409
 * (`NOT_ACTIVE: …`, plain string `detail`) passes through verbatim. Body:
 * optional `{reason?: string}`.
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
      backendUrl(`/deployments/${encodeURIComponent(deploymentId)}/pause`),
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
