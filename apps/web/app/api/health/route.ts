import { NextResponse } from "next/server";
import { backendUrl } from "@/lib/backend";

/** No auth required by the backend contract; still proxied so the browser
 * only ever talks to same-origin Next.js routes. */
export async function GET() {
  try {
    const r = await fetch(backendUrl("/health"), { cache: "no-store" });
    const data = await r.json().catch(() => null);
    return NextResponse.json(data, { status: r.status });
  } catch {
    return NextResponse.json(
      { detail: "DATA_UNAVAILABLE: could not reach the trading API" },
      { status: 503 },
    );
  }
}
