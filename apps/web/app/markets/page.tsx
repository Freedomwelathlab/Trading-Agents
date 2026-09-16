import type { Metadata } from "next";
import MarketsTerminal, { DEFAULT_SYMBOL } from "@/components/markets/MarketsTerminal";

export const metadata: Metadata = {
  title: "Markets · Trading OS",
};

/**
 * The Markets terminal (Phase 74, D092).
 *
 * A server component that renders one client island, matching how every
 * other page here is built: the data all comes from authenticated proxy
 * routes that need the session cookie, so fetching it on the server would
 * mean threading the cookie through a second path for no benefit.
 *
 * It does resolve `?symbol=` itself, though. That has to happen here: the
 * client island cannot read the query during SSR, so deriving it there
 * renders the default on the server and the real symbol in the browser —
 * a hydration mismatch rather than a cosmetic one.
 */
export default async function MarketsPage({
  searchParams,
}: {
  searchParams: Promise<{ symbol?: string }>;
}) {
  const { symbol } = await searchParams;
  const initial = symbol?.trim() ? symbol.trim().toUpperCase() : DEFAULT_SYMBOL;
  return (
    <main className="mx-auto flex w-full max-w-[1400px] flex-col gap-5 px-4 py-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight">Markets</h1>
        <p className="max-w-[70ch] text-sm text-[var(--ink-muted)]">
          Live quotes, this platform&rsquo;s own bars, and the session levels every intraday
          setup is measured against. Everything on screen is data this system holds or a
          vendor returned — an absent level shows as a dash, never as a zero.
        </p>
      </header>
      <MarketsTerminal initialSymbol={initial} />
    </main>
  );
}
