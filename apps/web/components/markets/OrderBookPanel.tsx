"use client";

import { useMemo } from "react";
import { Alert, EmptyNote, Panel, Pill } from "@/components/ui/primitives";
import { classifyWithAbsences, useKeyedFetch } from "@/lib/useKeyedFetch";

/**
 * The live order-book ladder (Phase 74, D092).
 *
 * **This component's real job is to never draw a book that is not there.**
 *
 * The vendor answers a depth request for a US symbol on this account with
 * HTTP 200 and a structurally valid book whose single bid and single ask
 * carry a null price and a zero volume. Measured live: `TQQQ.US` and
 * `AAPL.US` both return that, while `700.HK` returns genuine levels
 * (434.200 x 5,700 against 434.400 x 7,800). Two explanations fit — an
 * LV1 entitlement that carries no ladder, or simply a market that was
 * closed at the time of measurement — and one observation cannot separate
 * them, so neither this component nor the backend claims to know which.
 *
 * The backend turns that empty book into a 404 DATA_UNAVAILABLE, and this
 * renders the 404 as an explicit absence. The failure mode being avoided
 * is specific: a ladder of `0.00 x 0` rows looks like a real market that
 * is merely thin, and a zero bid with a zero spread is a number a reader
 * would act on.
 *
 * Volume bars are scaled against the largest level SHOWN, not against a
 * fixed maximum, so the shape of the book is readable whether the top
 * level is 50 shares or 50,000.
 */

type DepthLevel = {
  price: string;
  volume: number;
  order_count: number | null;
};

type OrderBook = {
  symbol: string;
  bids: DepthLevel[];
  asks: DepthLevel[];
  spread: string | null;
  as_of: string;
  source: string;
};

const REFRESH_MS = 5_000;

export default function OrderBookPanel({
  symbol,
  className,
}: {
  symbol: string;
  className?: string;
}) {
  const classify = useMemo(
    // 404 and 503 are ABSENCES, not errors — the vendor answered and had
    // nothing, or no vendor is wired. Rendering either in red would
    // suggest something is broken when nothing is.
    () => classifyWithAbsences<OrderBook>([404, 503], "No order book available."),
    [],
  );

  const { data: book, unavailable, error, loading } = useKeyedFetch<OrderBook>({
    key: symbol,
    url: `/api/market-data/${encodeURIComponent(symbol)}/depth`,
    classify,
    refreshMs: REFRESH_MS,
  });

  const peak = useMemo(() => {
    const levels = [...(book?.bids ?? []), ...(book?.asks ?? [])];
    return levels.reduce((m, l) => Math.max(m, l.volume), 0);
  }, [book]);

  return (
    <Panel
      className={className}
      title="Order book"
      description={
        book
          ? `${book.symbol} · ${book.source} · as of ${new Date(book.as_of).toLocaleTimeString()}`
          : symbol
      }
    >
      {error ? <Alert>{error}</Alert> : null}

      {loading ? <EmptyNote>Loading the book…</EmptyNote> : null}

      {unavailable ? (
        <div className="flex flex-col gap-2">
          <Pill tone="neutral">DATA_UNAVAILABLE</Pill>
          <p className="text-xs leading-relaxed text-ink-faint">{unavailable}</p>
          <p className="text-xs leading-relaxed text-ink-faint">
            A book is not drawn when the vendor has no priced level. That happens when the
            market is closed, and also when the account&rsquo;s quote entitlement carries no
            depth ladder — one response cannot tell those apart, so neither is asserted here.
          </p>
        </div>
      ) : null}

      {book && book.bids.length === 0 && book.asks.length === 0 ? (
        <EmptyNote>The vendor returned a book with no levels on either side.</EmptyNote>
      ) : null}

      {book && (book.bids.length > 0 || book.asks.length > 0) ? (
        <div className="flex flex-col gap-3">
          <div className="flex items-baseline justify-between text-xs">
            <span className="text-ink-faint">Spread</span>
            <span className="font-mono tabular-nums">
              {/* Null, not zero: a one-sided book genuinely has no spread. */}
              {book.spread ?? <span className="text-ink-faint">— one-sided</span>}
            </span>
          </div>

          <Ladder side="ask" levels={[...book.asks].reverse()} peak={peak} />
          <Ladder side="bid" levels={book.bids} peak={peak} />
        </div>
      ) : null}
    </Panel>
  );
}

function Ladder({
  side,
  levels,
  peak,
}: {
  side: "bid" | "ask";
  levels: DepthLevel[];
  peak: number;
}) {
  if (levels.length === 0) {
    return <p className="text-xs text-ink-faint">No {side} levels.</p>;
  }
  const tone = side === "bid" ? "var(--pos)" : "var(--neg)";
  return (
    <ul className="flex flex-col gap-px">
      {levels.map((lvl, i) => (
        <li
          key={`${side}-${lvl.price}-${i}`}
          className="relative flex items-center justify-between px-2 py-1 font-mono text-xs tabular-nums"
        >
          <span
            aria-hidden
            className="absolute inset-y-0 right-0 rounded-sm opacity-15"
            style={{
              background: tone,
              // Scaled against the largest level SHOWN, so the shape of the
              // book reads whether the top level is 50 shares or 50,000.
              width: peak > 0 ? `${Math.max(2, (lvl.volume / peak) * 100)}%` : "0%",
            }}
          />
          <span className="relative" style={{ color: tone }}>
            {lvl.price}
          </span>
          <span className="relative text-ink-faint">
            {lvl.volume.toLocaleString()}
            {lvl.order_count !== null ? ` · ${lvl.order_count}` : ""}
          </span>
        </li>
      ))}
    </ul>
  );
}
