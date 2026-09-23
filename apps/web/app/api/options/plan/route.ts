import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `POST /options/plan` (Phase 91, D110) — the playbook's decision
 * layer over HTTP.
 *
 * Pure pass-through, status included. A refusal comes back as a 200 with
 * `planned: false`, and this must not reinterpret it: "the signal did not
 * clear the score floor" is the decision layer working, and turning it
 * into an error here would make a correct answer look like a failure.
 *
 * The route plans and never trades — it reaches no broker and writes
 * nothing.
 */
export async function POST(request: NextRequest) {
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }
  const body = await request.text();

  let r: Response;
  try {
    r = await fetch(backendUrl("/options/plan"), {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
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
