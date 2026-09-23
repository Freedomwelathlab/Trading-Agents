import type { Metadata } from "next";
import OptionChainTerminal from "@/components/options/OptionChainTerminal";

export const metadata: Metadata = {
  title: "Options · Trading OS",
};

/**
 * The options desk (Phase 89, D108).
 *
 * A server component resolving `?symbol=` before the first byte, for the
 * same reason the Markets page does: the client island cannot read the
 * query during SSR, so deriving it there renders the default on the
 * server and the real symbol in the browser — a hydration mismatch.
 */
export default async function OptionsPage({
  searchParams,
}: {
  searchParams: Promise<{ symbol?: string }>;
}) {
  const { symbol } = await searchParams;
  const initial = symbol?.trim() ? symbol.trim().toUpperCase() : "TQQQ.US";
  return (
    <main className="mx-auto flex w-full max-w-[1400px] flex-col gap-5 px-4 py-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight">Options</h1>
        <p className="max-w-[70ch] text-sm text-[var(--ink-muted)]">
          The live contract ladder for an underlying, strike by strike. Strikes come from
          the vendor&rsquo;s own listing and the market on each contract from its quote; a
          field the vendor did not return shows as a dash, never as a price of zero.
        </p>
      </header>
      <OptionChainTerminal initialSymbol={initial} />
    </main>
  );
}
