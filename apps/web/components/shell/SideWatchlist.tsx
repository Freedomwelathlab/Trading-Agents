"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { classifyWithAbsences, useKeyedFetch } from "@/lib/useKeyedFetch";
import { handleExpiredSession } from "@/lib/session";
import { btnGhost } from "@/components/ui/primitives";

/**
 * The watchlist, always in reach: a compact rail in the left panel on
 * every page (Phase 80).
 *
 * Every row is one the backend returned. A symbol the vendor cannot price
 * keeps its row and shows the backend's own `unavailable` sentinel as an
 * em dash with the reason on hover — it is never dropped (which would
 * quietly shorten the user's own list) and never given a stand-in price.
 * Prices refresh every 10 s, the same polling cadence the Markets terminal
 * uses; there is no streaming feed in this platform (docs/AUDIT.md gap D).
 *
 * Clicking a symbol opens it on the Markets terminal. Adding and removing
 * symbols happens here too so the list is maintained where it is read;
 * the full watchlist manager (create/delete lists) stays on the dashboard.
 */

const REFRESH_MS = 10_000;

type WatchlistRow = { id: string; name: string; symbols: string[] };
type QuoteRow = {
  symbol: string;
  price: string | null;
  as_of: string | null;
  unavailable: string | null;
};

export default function SideWatchlist() {
  const router = useRouter();
  const [activeList, setActiveList] = useState<string | null>(null);
  const [newSymbol, setNewSymbol] = useState("");
  const [newListName, setNewListName] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const classifyLists = useMemo(
    () => classifyWithAbsences<{ watchlists: WatchlistRow[] }>([404], "No watchlists."),
    [],
  );
  const {
    data: listsPayload,
    error: listsError,
    reload: reloadLists,
  } = useKeyedFetch<{ watchlists: WatchlistRow[] }>({
    key: "side-watchlists",
    url: "/api/watchlists?limit=50&offset=0",
    classify: classifyLists,
  });
  const lists = listsPayload?.watchlists ?? null;

  // Derived, not stored: the explicit choice wins while it still exists,
  // otherwise the first list. Same reasoning as the Markets rail (D092).
  const selected =
    (activeList && lists?.some((l) => l.id === activeList) ? activeList : null) ??
    lists?.[0]?.id ??
    null;

  const classifyQuotes = useMemo(
    () => classifyWithAbsences<{ quotes: QuoteRow[] }>([404], "No quotes."),
    [],
  );
  const {
    data: quotesPayload,
    unavailable: quotesUnavailable,
    reload: reloadQuotes,
  } = useKeyedFetch<{ quotes: QuoteRow[] }>({
    key: `side-quotes:${selected ?? "none"}`,
    url: selected ? `/api/watchlists/${encodeURIComponent(selected)}/quotes` : "",
    classify: classifyQuotes,
    refreshMs: REFRESH_MS,
    enabled: selected !== null,
  });
  const quotes = quotesPayload?.quotes ?? null;

  async function post(url: string, body: unknown, method = "POST"): Promise<boolean> {
    setActionError(null);
    setBusy(true);
    try {
      const res = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (handleExpiredSession(res.status)) return false;
      if (!res.ok && res.status !== 204) {
        const data = (await res.json().catch(() => null)) as { detail?: string } | null;
        setActionError(data?.detail ?? `Request failed (HTTP ${res.status})`);
        return false;
      }
      return true;
    } catch {
      setActionError("DATA_UNAVAILABLE: could not reach the trading API");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function addSymbol(e: React.FormEvent) {
    e.preventDefault();
    const symbol = newSymbol.trim().toUpperCase();
    if (!symbol || !selected) return;
    if (await post(`/api/watchlists/${encodeURIComponent(selected)}/items`, { symbol })) {
      setNewSymbol("");
      reloadLists();
      reloadQuotes();
    }
  }

  async function removeSymbol(symbol: string) {
    if (!selected) return;
    const ok = await post(
      `/api/watchlists/${encodeURIComponent(selected)}/items/${encodeURIComponent(symbol)}`,
      undefined,
      "DELETE",
    );
    if (ok) {
      reloadLists();
      reloadQuotes();
    }
  }

  async function createList(e: React.FormEvent) {
    e.preventDefault();
    const name = newListName.trim();
    if (!name) return;
    if (await post("/api/watchlists", { name })) {
      setNewListName("");
      reloadLists();
    }
  }

  return (
    <section aria-labelledby="side-watchlist-heading" className="flex min-h-0 flex-col gap-2">
      <div className="flex items-center justify-between px-1">
        <h2
          id="side-watchlist-heading"
          className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-faint"
        >
          Watchlist
        </h2>
        {lists && lists.length > 1 && selected ? (
          <select
            id="side-watchlist-select"
            aria-label="Active watchlist"
            value={selected}
            onChange={(e) => setActiveList(e.target.value)}
            className="max-w-[120px] rounded border border-line bg-surface px-1 py-0.5 text-[11px] text-ink-muted"
          >
            {lists.map((l) => (
              <option key={l.id} value={l.id}>
                {l.name}
              </option>
            ))}
          </select>
        ) : lists && lists.length === 1 ? (
          <span className="truncate text-[11px] text-ink-muted">{lists[0].name}</span>
        ) : null}
      </div>

      {listsError ? (
        <p className="px-1 text-xs text-neg">{listsError}</p>
      ) : lists === null ? (
        <p className="px-1 text-xs text-ink-faint">Loading…</p>
      ) : lists.length === 0 ? (
        <form onSubmit={createList} className="flex flex-col gap-1.5 px-1">
          <p className="text-xs text-ink-faint">No watchlist yet.</p>
          <input
            id="side-watchlist-new-name"
            aria-label="New watchlist name"
            value={newListName}
            onChange={(e) => setNewListName(e.target.value)}
            placeholder="Name, e.g. US intraday"
            className="rounded border border-line bg-surface px-2 py-1 text-xs"
          />
          <button type="submit" disabled={busy || !newListName.trim()} className={btnGhost}>
            Create watchlist
          </button>
        </form>
      ) : (
        <>
          {quotesUnavailable ? (
            <p className="px-1 text-[11px] text-ink-faint" title={quotesUnavailable}>
              Prices unavailable: {quotesUnavailable}
            </p>
          ) : null}
          <ul className="flex max-h-64 flex-col overflow-y-auto" aria-label="Watchlist symbols">
            {(quotes ?? lists.find((l) => l.id === selected)?.symbols.map((symbol) => ({
              symbol,
              price: null,
              as_of: null,
              unavailable: null,
            })) ?? []).map((q) => (
              <li key={q.symbol} className="group flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => router.push(`/markets?symbol=${encodeURIComponent(q.symbol)}`)}
                  className="flex min-w-0 flex-1 items-center justify-between rounded-md px-2 py-1 text-left font-mono text-xs hover:bg-well focus-visible:outline-2 focus-visible:outline-accent"
                  title={`Open ${q.symbol} on the Markets terminal`}
                >
                  <span className="truncate">{q.symbol}</span>
                  {q.price !== null ? (
                    <span className="tabular-nums text-ink">{q.price}</span>
                  ) : (
                    <span className="text-ink-faint" title={q.unavailable ?? "no price yet"}>
                      —
                    </span>
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => void removeSymbol(q.symbol)}
                  disabled={busy}
                  aria-label={`Remove ${q.symbol}`}
                  className="rounded px-1 text-xs text-ink-faint opacity-0 hover:text-neg focus-visible:opacity-100 group-hover:opacity-100"
                >
                  ×
                </button>
              </li>
            ))}
            {quotes !== null && quotes.length === 0 ? (
              <li className="px-2 py-1 text-xs text-ink-faint">Empty — add a symbol below.</li>
            ) : null}
          </ul>
          <form onSubmit={addSymbol} className="flex gap-1 px-1">
            <input
              id="side-watchlist-add"
              aria-label="Add symbol"
              value={newSymbol}
              onChange={(e) => setNewSymbol(e.target.value)}
              placeholder="TQQQ.US"
              className="min-w-0 flex-1 rounded border border-line bg-surface px-2 py-1 font-mono text-xs uppercase"
            />
            <button type="submit" disabled={busy || !newSymbol.trim()} className={btnGhost}>
              Add
            </button>
          </form>
        </>
      )}
      {actionError ? <p className="px-1 text-xs text-neg">{actionError}</p> : null}
    </section>
  );
}
