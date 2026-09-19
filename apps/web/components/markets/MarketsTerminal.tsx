"use client";

import { useMemo, useState } from "react";
import { classifyWithAbsences, useKeyedFetch } from "@/lib/useKeyedFetch";
import { PriceChart, type PriceBar } from "@/components/PriceChart";
import OrderBookPanel from "@/components/markets/OrderBookPanel";
import SessionLevelsPanel, {
  toPriceLines,
  useSessionLevels,
} from "@/components/markets/SessionLevelsPanel";
import {
  Alert,
  EmptyNote,
  Panel,
  Pill,
  btnGhost,
  btnSecondary,
  inputClass,
} from "@/components/ui/primitives";

/**
 * The Markets terminal (Phase 74, D092).
 *
 * A symbol rail, a candlestick chart with this platform's own session
 * levels drawn on it, the live order book, and the level table — the
 * TradingView-shaped surface the brief asked for, built on THIS system's
 * bars rather than an embedded widget showing someone else's.
 *
 * Three decisions worth stating, because each has an easier alternative
 * that would quietly misrepresent something.
 *
 * **1. "Live" here means polled, and the page says so.** There is no
 * websocket anywhere in this platform, so the quote and the book refresh
 * on an interval and every panel carries the timestamp of the data it is
 * showing. Labelling a 5-second poll "real-time streaming" would be a
 * claim about latency this system does not meet.
 *
 * **2. The chart and the level table read the same response.** The lines
 * drawn on the chart come from `toPriceLines(levels)` — the very object
 * the table renders — so a number on screen and a line on the chart
 * cannot disagree.
 *
 * **3. Bars come from the store, not from a vendor call.** The chart shows
 * what this platform has actually ingested. A symbol with no stored bars
 * gets an explicit "backfill this first" state rather than an empty chart
 * that looks like a flat market.
 */

const INTERVALS = ["1m", "5m", "15m", "30m", "1h", "1d"] as const;
type Interval = (typeof INTERVALS)[number];

/** Lookback per interval, chosen so each shows a readable window rather
 *  than a fixed day count that is 3 bars at 1d and 20,000 at 1m. */
const LOOKBACK_DAYS: Record<Interval, number> = {
  "1m": 3,
  "5m": 10,
  "15m": 30,
  "30m": 60,
  "1h": 120,
  "1d": 365,
};

const QUOTE_REFRESH_MS = 5_000;

type Quote = { symbol: string; price: string; as_of: string; source: string };

type WatchlistSummary = { id: string; name: string };
type WatchlistQuote = {
  symbol: string;
  price: string | null;
  as_of: string | null;
  unavailable: string | null;
};

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

export const DEFAULT_SYMBOL = "TQQQ.US";

export default function MarketsTerminal({
  initialSymbol = DEFAULT_SYMBOL,
}: {
  /**
   * Resolved from `?symbol=` by the SERVER component that renders this.
   *
   * Reading `window.location.search` here instead looks equivalent and is
   * not: the server has no `window`, so it renders the default while the
   * client renders the URL's symbol, and React reports a hydration
   * mismatch (observed — the page rendered "TQQQ.US" on the server and
   * "700.HK" in the browser). The server page already receives
   * `searchParams`, so the value is known before the first byte and both
   * passes agree.
   */
  initialSymbol?: string;
}) {
  const [symbol, setSymbolState] = useState(initialSymbol);
  const [draft, setDraft] = useState(symbol);

  /** Selecting a symbol also puts it in the URL, so a terminal view is a
   *  link somebody can send. `replaceState` rather than a router push:
   *  flipping symbols should not stack history entries to back out of. */
  const setSymbol = (next: string) => {
    setSymbolState(next);
    setDraft(next);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("symbol", next);
      window.history.replaceState(null, "", url);
    }
  };
  const [interval, setIntervalChoice] = useState<Interval>("5m");

  // --- bars -------------------------------------------------------------
  const barsUrl = useMemo(() => {
    const qs = new URLSearchParams({
      start_date: isoDaysAgo(LOOKBACK_DAYS[interval]),
      end_date: new Date().toISOString().slice(0, 10),
      bar_interval: interval,
    });
    return `/api/market-data/${encodeURIComponent(symbol)}/bars?${qs}`;
  }, [symbol, interval]);

  const classifyBars = useMemo(
    () => classifyWithAbsences<{ bars: PriceBar[] }>([404], "No bars stored for this symbol."),
    [],
  );

  const {
    data: barsPayload,
    unavailable: barsUnavailable,
    error: barsError,
    loading: loadingBars,
  } = useKeyedFetch<{ bars: PriceBar[] }>({
    key: `${symbol}|${interval}`,
    url: barsUrl,
    classify: classifyBars,
  });
  const bars = barsPayload?.bars ?? [];

  // --- quote ------------------------------------------------------------
  const classifyQuote = useMemo(
    () => classifyWithAbsences<Quote>([404, 503], "No quote available."),
    [],
  );

  const { data: quote, unavailable: quoteUnavailable } = useKeyedFetch<Quote>({
    key: symbol,
    // The quote proxy predates the market-data folder and lives at its
    // own top-level path — not under /api/market-data.
    url: `/api/quote/${encodeURIComponent(symbol)}`,
    classify: classifyQuote,
    refreshMs: QUOTE_REFRESH_MS,
  });

  const { levels, unavailable: levelsUnavailable, error: levelsError } = useSessionLevels(
    symbol,
    interval === "1d" ? "5m" : interval,
  );

  const priceLines = useMemo(() => toPriceLines(levels), [levels]);

  return (
    <div className="flex flex-col gap-4">
      <Panel
        title={symbol}
        description={
          quote
            ? `${quote.price} · ${quote.source} · as of ${new Date(quote.as_of).toLocaleString()}`
            : (quoteUnavailable ?? "Loading quote…")
        }
        actions={
          <form
            className="flex items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              const next = draft.trim().toUpperCase();
              if (next) setSymbol(next);
            }}
          >
            <label className="sr-only" htmlFor="markets-symbol">
              Symbol
            </label>
            <input
              id="markets-symbol"
              className={`${inputClass} w-40 font-mono`}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="TQQQ.US"
              aria-describedby="markets-symbol-hint"
            />
            <button type="submit" className={btnSecondary}>
              Load
            </button>
          </form>
        }
      >
        <p id="markets-symbol-hint" className="text-xs text-ink-faint">
          Longbridge symbols carry a market suffix (<code>TQQQ.US</code>, <code>700.HK</code>).
          Quote and book refresh every {QUOTE_REFRESH_MS / 1000}s by polling — this platform has
          no streaming socket, so nothing here is claimed to be tick-by-tick.
        </p>

        <WatchlistRail activeSymbol={symbol} onPick={setSymbol} />
      </Panel>

      <div className="grid gap-4 xl:grid-cols-12">
        <Panel
          className="xl:col-span-8"
          title="Chart"
          description={`${interval} · ${bars.length} bars${priceLines.length ? ` · ${priceLines.length} session levels drawn` : ""}`}
          actions={
            <div className="flex flex-wrap gap-1" role="group" aria-label="Bar interval">
              {INTERVALS.map((iv) => (
                <button
                  key={iv}
                  type="button"
                  onClick={() => setIntervalChoice(iv)}
                  aria-pressed={iv === interval}
                  className={iv === interval ? btnSecondary : btnGhost}
                >
                  {iv}
                </button>
              ))}
            </div>
          }
        >
          {barsError ? <Alert>{barsError}</Alert> : null}
          {loadingBars && bars.length === 0 ? <EmptyNote>Loading bars…</EmptyNote> : null}
          {(barsUnavailable ?? (barsPayload && bars.length === 0 ? "No bars stored for this window." : null)) &&
          bars.length === 0 ? (
            <div className="flex flex-col gap-2">
              <Pill tone="neutral">DATA_UNAVAILABLE</Pill>
              <p className="text-xs leading-relaxed text-ink-faint">
                {barsUnavailable ?? "No bars stored for this window."}
              </p>
              <p className="text-xs text-ink-faint leading-relaxed">
                An empty chart would look like a flat market. Ingest this symbol with
                <code className="mx-1">POST /admin/market-data/backfill</code>
                and it will appear here.
              </p>
            </div>
          ) : null}
          {bars.length > 0 ? (
            <PriceChart bars={bars} priceLines={priceLines} height={420} />
          ) : null}
        </Panel>

        <div className="xl:col-span-4 flex flex-col gap-4">
          <OrderBookPanel symbol={symbol} />
          <SessionLevelsPanel
            levels={levels}
            unavailable={levelsUnavailable}
            error={levelsError}
          />
        </div>
      </div>
    </div>
  );
}

/**
 * Watchlist tabs across the top of the terminal.
 *
 * Reuses the watchlists the dashboard already manages rather than
 * introducing a second, parallel notion of "the symbols I follow". A
 * symbol the vendor could not price keeps its chip and shows the
 * backend's own unavailable sentinel — it is never dropped, which would
 * quietly shorten the user's own list.
 */
function WatchlistRail({
  activeSymbol,
  onPick,
}: {
  activeSymbol: string;
  onPick: (symbol: string) => void;
}) {
  const [activeList, setActiveList] = useState<string | null>(null);

  const classifyLists = useMemo(
    () => classifyWithAbsences<{ watchlists: WatchlistSummary[] }>([404], "No watchlists."),
    [],
  );
  const { data: listsPayload } = useKeyedFetch<{ watchlists: WatchlistSummary[] }>({
    key: "watchlists",
    url: "/api/watchlists",
    classify: classifyLists,
  });
  const lists = listsPayload?.watchlists ?? null;

  // The selected list is DERIVED from what came back, with the explicit
  // choice taking precedence when it is still present. Storing the default
  // via an effect would be another setState-in-effect, and it would also
  // fight the user's click for one render.
  const selected =
    (activeList && lists?.some((l) => l.id === activeList) ? activeList : null) ??
    lists?.[0]?.id ??
    null;

  const classifyQuotes = useMemo(
    () => classifyWithAbsences<{ quotes: WatchlistQuote[] }>([404], "No quotes."),
    [],
  );
  const { data: quotesPayload } = useKeyedFetch<{ quotes: WatchlistQuote[] }>({
    key: selected ?? "none",
    url: selected ? `/api/watchlists/${selected}/quotes` : "",
    classify: classifyQuotes,
    enabled: selected !== null,
  });
  const quotes = quotesPayload?.quotes ?? [];

  if (lists !== null && lists.length === 0) {
    return (
      <p className="text-xs text-ink-faint">
        No watchlists yet — create one on the dashboard and its symbols appear here.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {lists && lists.length > 1 ? (
        <div className="flex flex-wrap gap-1" role="tablist" aria-label="Watchlists">
          {lists.map((l) => (
            <button
              key={l.id}
              type="button"
              role="tab"
              aria-selected={l.id === selected}
              onClick={() => setActiveList(l.id)}
              className={l.id === selected ? btnSecondary : btnGhost}
            >
              {l.name}
            </button>
          ))}
        </div>
      ) : null}

      {quotes.length > 0 ? (
        <ul className="flex flex-wrap gap-1.5">
          {quotes.map((q) => (
            <li key={q.symbol}>
              <button
                type="button"
                onClick={() => onPick(q.symbol)}
                aria-current={q.symbol === activeSymbol}
                className={`rounded-md border px-2.5 py-1 font-mono text-xs transition ${
                  q.symbol === activeSymbol
                    ? "border-accent bg-well"
                    : "border-line hover:bg-well"
                }`}
              >
                <span>{q.symbol}</span>{" "}
                {q.price !== null ? (
                  <span className="tabular-nums">{q.price}</span>
                ) : (
                  /* The row keeps its chip. Dropping an unpriceable symbol
                     would quietly shorten the user's own list. */
                  <span className="text-ink-faint" title={q.unavailable ?? undefined}>
                    —
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
