"use client";

import { useMemo, useState } from "react";
import { classifyWithAbsences, useKeyedFetch } from "@/lib/useKeyedFetch";
import OrderBookPanel from "@/components/markets/OrderBookPanel";
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
 * The option chain desk (Phase 89, D108).
 *
 * A strike ladder with calls on the left and puts on the right, which is
 * how a chain is read, and three deliberate refusals to smooth anything
 * over:
 *
 * **A dash is a dash.** Every market field is nullable and a null means
 * the vendor returned nothing for that contract. Rendering `0.00` would
 * put a tradeable-looking price on a contract nobody quoted.
 *
 * **The quoted count is shown, not buried.** A 300-row chain with 12
 * quoted rows is a chain nobody should price a spread from. The header
 * says how many came back with any market at all.
 *
 * **The at-the-money row is marked from the live quote, not guessed.**
 * When no quote is available the highlight is simply absent rather than
 * placed at the middle of the ladder, which would look identical and mean
 * something else entirely.
 */

type OptionQuote = {
  contract_symbol: string;
  strike: string;
  right: string;
  last_price: string | null;
  bid: string | null;
  ask: string | null;
  mid: string | null;
  spread: string | null;
  volume: number | null;
  open_interest: number | null;
  implied_vol: string | null;
  delta: string | null;
  gamma: string | null;
  theta: string | null;
  vega: string | null;
};

type Chain = {
  symbol: string;
  expiry: string;
  as_of: string;
  source: string;
  calls: OptionQuote[];
  puts: OptionQuote[];
  quoted_contracts: number;
  note: string;
};

type Expiries = { symbol: string; expiries: string[]; source: string };
type Quote = { symbol: string; price: string; as_of: string; source: string };

function n(v: string | null | undefined, digits = 2): string {
  if (v == null) return "—";
  const x = Number(v);
  return Number.isFinite(x) ? x.toFixed(digits) : "—";
}

function pct(v: string | null | undefined): string {
  if (v == null) return "—";
  const x = Number(v);
  // Vendors quote implied volatility as a fraction; 0.42 is 42%.
  return Number.isFinite(x) ? `${(x * 100).toFixed(1)}%` : "—";
}

function int(v: number | null | undefined): string {
  return v == null ? "—" : v.toLocaleString();
}

/** One side of one strike. `align` decides which way the columns read —
 *  calls mirror outward from the strike, the way a paper chain does. */
function Side({ q, align }: { q: OptionQuote | undefined; align: "left" | "right" }) {
  const cells = [
    n(q?.bid),
    n(q?.ask),
    n(q?.last_price),
    pct(q?.implied_vol),
    n(q?.delta, 3),
    int(q?.volume),
    int(q?.open_interest),
  ];
  const ordered = align === "right" ? [...cells].reverse() : cells;
  return (
    <>
      {ordered.map((c, i) => (
        <td
          key={i}
          className={`px-1.5 py-1 tabular-nums ${c === "—" ? "text-ink-faint" : ""} ${
            align === "right" ? "text-right" : ""
          }`}
        >
          {c}
        </td>
      ))}
    </>
  );
}

const CALL_HEADS = ["bid", "ask", "last", "IV", "Δ", "vol", "OI"];

export default function OptionChainTerminal({
  initialSymbol,
}: {
  initialSymbol: string;
}) {
  const [symbol, setSymbol] = useState(initialSymbol);
  const [draft, setDraft] = useState(initialSymbol);
  const [expiry, setExpiry] = useState<string | null>(null);

  const classifyExpiries = useMemo(
    () => classifyWithAbsences<Expiries>([404], "No expiries listed for this symbol."),
    [],
  );
  const { data: expiriesPayload, unavailable: expiriesUnavailable, error: expiriesError } =
    useKeyedFetch<Expiries>({
      key: `exp:${symbol}`,
      url: `/api/market-data/${encodeURIComponent(symbol)}/option-expiries`,
      classify: classifyExpiries,
    });
  const expiries = expiriesPayload?.expiries ?? [];
  // Derived, not stored in an effect: the chosen expiry wins while it is
  // still listed, otherwise the nearest one.
  const selected = (expiry && expiries.includes(expiry) ? expiry : null) ?? expiries[0] ?? null;

  const classifyChain = useMemo(
    () => classifyWithAbsences<Chain>([404], "No contracts listed for this expiry."),
    [],
  );
  const { data: chain, unavailable: chainUnavailable, error: chainError, loading } =
    useKeyedFetch<Chain>({
      key: `chain:${symbol}|${selected ?? "none"}`,
      url: selected
        ? `/api/market-data/${encodeURIComponent(symbol)}/option-chain?expiry=${selected}`
        : "",
      classify: classifyChain,
      enabled: selected !== null,
    });

  const classifyQuote = useMemo(
    () => classifyWithAbsences<Quote>([404, 503], "No quote available."),
    [],
  );
  const { data: quote } = useKeyedFetch<Quote>({
    key: `q:${symbol}`,
    url: `/api/quote/${encodeURIComponent(symbol)}`,
    classify: classifyQuote,
    refreshMs: 5_000,
  });

  const spot = quote ? Number(quote.price) : null;

  const rows = useMemo(() => {
    if (!chain) return [];
    const byStrike = new Map<string, { call?: OptionQuote; put?: OptionQuote }>();
    for (const c of chain.calls) {
      byStrike.set(c.strike, { ...(byStrike.get(c.strike) ?? {}), call: c });
    }
    for (const p of chain.puts) {
      byStrike.set(p.strike, { ...(byStrike.get(p.strike) ?? {}), put: p });
    }
    return [...byStrike.entries()]
      .map(([strike, sides]) => ({ strike, ...sides }))
      .sort((a, b) => Number(a.strike) - Number(b.strike));
  }, [chain]);

  // The strike nearest the live price, or null when there is no live
  // price. Never the middle of the ladder as a stand-in.
  const atmStrike = useMemo(() => {
    if (spot == null || rows.length === 0) return null;
    return rows.reduce((best, r) =>
      Math.abs(Number(r.strike) - spot) < Math.abs(Number(best.strike) - spot) ? r : best,
    ).strike;
  }, [rows, spot]);

  return (
    <div className="flex flex-col gap-4">
      <Panel
        title={symbol}
        description={
          quote
            ? `${quote.price} · ${quote.source} · as of ${new Date(quote.as_of).toLocaleString()}`
            : "No live quote — the at-the-money row is not marked without one."
        }
        actions={
          <form
            className="flex items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              const next = draft.trim().toUpperCase();
              if (next) {
                setSymbol(next);
                setExpiry(null);
              }
            }}
          >
            <label className="sr-only" htmlFor="options-symbol">
              Underlying
            </label>
            <input
              id="options-symbol"
              className={`${inputClass} w-40 font-mono`}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="TQQQ.US"
            />
            <button type="submit" className={btnSecondary}>
              Load
            </button>
          </form>
        }
      >
        {expiriesError ? <Alert>{expiriesError}</Alert> : null}
        {expiriesUnavailable && expiries.length === 0 ? (
          <EmptyNote>{expiriesUnavailable}</EmptyNote>
        ) : null}
        {expiriesPayload && expiries.length === 0 ? (
          <EmptyNote>
            {expiriesPayload.source} lists no option contracts on {symbol}. That is the
            vendor&rsquo;s own answer, not a failed request.
          </EmptyNote>
        ) : null}
        {expiries.length > 0 ? (
          <div className="flex flex-col gap-2">
            <p className="text-xs uppercase tracking-[0.08em] text-ink-faint">Expiry</p>
            <div
              className="flex max-h-24 flex-wrap gap-1 overflow-y-auto"
              role="group"
              aria-label="Expiry"
            >
              {expiries.map((d) => (
                <button
                  key={d}
                  type="button"
                  onClick={() => setExpiry(d)}
                  aria-pressed={d === selected}
                  className={d === selected ? btnSecondary : btnGhost}
                >
                  {d}
                </button>
              ))}
            </div>
          </div>
        ) : null}
      </Panel>

      <div className="grid gap-4 xl:grid-cols-12">
        <Panel
          className="xl:col-span-9"
          title="Option chain"
          description={
            chain
              ? `${chain.expiry} · ${rows.length} strikes · ${chain.quoted_contracts} of ${
                  chain.calls.length + chain.puts.length
                } contracts quoted · ${chain.source}`
              : undefined
          }
        >
          {chainError ? <Alert>{chainError}</Alert> : null}
          {loading && !chain ? <EmptyNote>Loading chain…</EmptyNote> : null}
          {chainUnavailable && !chain ? (
            <div className="flex flex-col gap-2">
              <Pill tone="neutral">DATA_UNAVAILABLE</Pill>
              <p className="text-xs leading-relaxed text-ink-faint">{chainUnavailable}</p>
            </div>
          ) : null}
          {chain ? (
            <div className="flex flex-col gap-2">
              {chain.quoted_contracts === 0 ? (
                <Alert>
                  Every contract in this expiry came back without a market. The ladder is
                  real; the prices are simply not there, and nothing here has been filled
                  in to make the table look complete.
                </Alert>
              ) : null}
              <div className="overflow-x-auto">
                <table className="w-full min-w-[900px] text-right text-[11px]">
                  <thead>
                    <tr className="border-b border-line uppercase tracking-[0.06em] text-ink-faint">
                      <th className="px-1.5 py-1 font-semibold" colSpan={7}>
                        calls
                      </th>
                      <th className="px-2 py-1 text-center font-semibold">strike</th>
                      <th className="px-1.5 py-1 text-left font-semibold" colSpan={7}>
                        puts
                      </th>
                    </tr>
                    <tr className="border-b border-line text-ink-faint">
                      {[...CALL_HEADS].reverse().map((h) => (
                        <th key={`c-${h}`} className="px-1.5 py-1 font-normal">
                          {h}
                        </th>
                      ))}
                      <th className="px-2 py-1" />
                      {CALL_HEADS.map((h) => (
                        <th key={`p-${h}`} className="px-1.5 py-1 text-left font-normal">
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {rows.map((r) => {
                      const atm = r.strike === atmStrike;
                      return (
                        <tr
                          key={r.strike}
                          data-testid={atm ? "atm-row" : undefined}
                          className={`border-b border-line/50 ${atm ? "bg-well font-semibold" : ""}`}
                        >
                          <Side q={r.call} align="right" />
                          <td className="whitespace-nowrap border-x border-line px-2 py-1 text-center tabular-nums">
                            {Number(r.strike).toFixed(2)}
                          </td>
                          <Side q={r.put} align="left" />
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <p className="text-[11px] leading-relaxed text-ink-faint">{chain.note}</p>
              {atmStrike === null && rows.length > 0 ? (
                <p className="text-[11px] leading-relaxed text-ink-faint">
                  No live quote for {symbol}, so no row is marked at the money. A highlight
                  placed at the middle of the ladder would look the same and mean something
                  else.
                </p>
              ) : null}
            </div>
          ) : null}
        </Panel>

        <div className="xl:col-span-3 flex flex-col gap-4">
          <OrderBookPanel symbol={symbol} />
        </div>
      </div>
    </div>
  );
}
