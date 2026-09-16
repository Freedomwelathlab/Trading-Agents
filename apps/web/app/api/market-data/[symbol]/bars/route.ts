import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, backendUrl } from "@/lib/backend";

/**
 * Proxies `GET /market-data/{symbol}/bars` (Phase 70/D088) — the persisted
 * OHLCV bars behind the candlestick chart on a backtest run's detail page.
 *
 * Query parameters are forwarded verbatim rather than being reconstructed
 * here: `start_date`, `end_date` and `bar_interval` are the backend's
 * contract, and rebuilding them in this layer would mean two places that
 * have to agree on the same names. An unknown `bar_interval` gets the
 * backend's own 422 rather than a second, differently-worded refusal.
 *
 * Same pass-through shape as every other proxy route: verbatim status and
 * body, no reshaping, no default-filling.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ symbol: string }> },
) {
  const { symbol } = await params;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  const query = request.nextUrl.searchParams.toString();
  const path = `/market-data/${encodeURIComponent(symbol)}/bars${query ? `?${query}` : ""}`;

  let r: Response;
  try {
    r = await fetch(backendUrl(path), {
      headers: { Authorization: `Bearer ${token}` },
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
