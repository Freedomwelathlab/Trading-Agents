import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /admin/users/{user_id}/password-reset` (docs/DECISIONS.md
 * D062). Same convention as the sibling PATCH handler: the httpOnly
 * session cookie becomes the backend's bearer token, and the backend's own
 * status and body are forwarded untouched.
 *
 * The `admin:manage` check lives in the backend and only in the backend —
 * this handler forwards whoever's cookie it was given and renders the real
 * 403 if that user does not hold the permission (D023's posture for every
 * admin route in this app).
 *
 * When no email provider is configured the forwarded body carries a real,
 * live `reset_link`. That is deliberate and is the whole point of the
 * endpoint, but it means this response must never be cached: `fetch`
 * defaults to no caching for POST, and nothing here adds any.
 */
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ userId: string }> },
) {
  const { userId } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  let r: Response;
  try {
    r = await fetch(
      backendUrl(`/admin/users/${encodeURIComponent(userId)}/password-reset`),
      {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
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
