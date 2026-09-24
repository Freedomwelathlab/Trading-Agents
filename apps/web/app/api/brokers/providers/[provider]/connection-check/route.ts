import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies the per-PROVIDER connection probe (Phase 95, D114).
 *
 * Distinct from the per-broker probe next door: this one answers "does
 * the key in this deployment's environment work" with no broker row and
 * no grant, which is the question an operator has before they have set
 * anything else up.
 *
 * Read-only at the backend — it calls `get_account_state` and nothing
 * else. As with the per-broker probe, a failed check is a 200 carrying
 * `reachable: false`, so this passes the backend's status through rather
 * than reinterpreting one.
 */
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ provider: string }> },
) {
  const { provider } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  let r: Response;
  try {
    r = await fetch(
      backendUrl(`/brokers/providers/${encodeURIComponent(provider)}/connection-check`),
      {
        method: "POST",
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
