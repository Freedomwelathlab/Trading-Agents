"use client";

import { useCallback, useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  EmptyNote,
  Field,
  Panel,
  Pill,
  TableScroll,
  btnGhost,
  btnPrimary,
  btnSecondary,
  inputClass,
  monoInputClass,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";

/**
 * Watchlists — the research half of the dashboard (Phase 50).
 *
 * Create a named list, put symbols on it, and read every symbol's live
 * quote in one table. Everything on screen is a row the backend returned:
 *
 * - A symbol the market-data vendor could not price keeps its row and
 *   renders the backend's own `DATA_UNAVAILABLE:` sentinel in place of a
 *   price. It is never dropped from the table (which would quietly
 *   shorten the user's own list) and never given a stand-in number.
 * - With no vendor wired at all, `market_data_configured` comes back
 *   false and every row is unavailable for that one reason, which is said
 *   once above the table rather than repeated N times inside it.
 * - A 401 redirects to login (D032); any other failure renders the
 *   backend's real `detail` string verbatim.
 *
 * There is no auto-created default watchlist, matching the backend: a
 * user who has created none sees an explicit empty state and a create
 * form, not a phantom list.
 *
 * Phase 45 / D060 design system throughout, and per D034's ordering
 * convention this panel is NOT broker-scoped — it sits with QuoteLookup
 * among the research tools, not inside the broker-scoped section.
 */

type WatchlistRow = {
  id: string;
  name: string;
  created_at: string;
  symbols: string[];
};

type QuoteRow = {
  symbol: string;
  price: string | null;
  as_of: string | null;
  source: string | null;
  unavailable: string | null;
};

type QuotesResponse = {
  watchlist_id: string;
  name: string;
  market_data_configured: boolean;
  quotes: QuoteRow[];
};

export default function Watchlist({ className }: { className?: string } = {}) {
  const [lists, setLists] = useState<WatchlistRow[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [newSymbol, setNewSymbol] = useState("");
  const [quotes, setQuotes] = useState<QuotesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [quotesLoading, setQuotesLoading] = useState(false);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  /** One place where a failed response becomes a rendered error, so every
   * action in this panel reports the backend's own detail string and
   * handles an expired session the same way. */
  function reportFailure(res: Response, data: Record<string, unknown> | null): boolean {
    if (handleExpiredSession(res.status)) return true;
    setStatus(res.status);
    setErrorDetail(
      (data?.detail as string | undefined) ?? `Request failed (HTTP ${res.status})`,
    );
    return true;
  }

  const loadLists = useCallback(async () => {
    setLoading(true);
    setErrorDetail(null);
    setStatus(null);
    try {
      const res = await fetch("/api/watchlists?limit=50&offset=0", { cache: "no-store" });
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      if (!res.ok) {
        reportFailure(res, data);
        return;
      }
      const rows = (data?.watchlists as WatchlistRow[] | undefined) ?? [];
      setLists(rows);
      setSelectedId((current) =>
        current && rows.some((r) => r.id === current) ? current : (rows[0]?.id ?? null),
      );
    } catch {
      setStatus(null);
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadLists();
    // Load once on mount; every later load is an explicit user action.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selected = lists?.find((l) => l.id === selectedId) ?? null;

  async function createList(e: React.FormEvent) {
    e.preventDefault();
    setErrorDetail(null);
    setStatus(null);
    try {
      const res = await fetch("/api/watchlists", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName }),
      });
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      if (!res.ok) {
        reportFailure(res, data);
        return;
      }
      setNewName("");
      setSelectedId((data?.id as string | undefined) ?? null);
      setQuotes(null);
      await loadLists();
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    }
  }

  async function addSymbol(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedId) return;
    setErrorDetail(null);
    setStatus(null);
    try {
      const res = await fetch(`/api/watchlists/${encodeURIComponent(selectedId)}/items`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: newSymbol }),
      });
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      if (!res.ok) {
        // A 409 (already on the list) is the backend's own message and is
        // shown as such — not swallowed into a silent no-op.
        reportFailure(res, data);
        return;
      }
      setNewSymbol("");
      setQuotes(null);
      await loadLists();
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    }
  }

  async function removeSymbol(symbol: string) {
    if (!selectedId) return;
    setErrorDetail(null);
    setStatus(null);
    try {
      const res = await fetch(
        `/api/watchlists/${encodeURIComponent(selectedId)}/items/${encodeURIComponent(symbol)}`,
        { method: "DELETE" },
      );
      if (!res.ok) {
        const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
        reportFailure(res, data);
        return;
      }
      setQuotes(null);
      await loadLists();
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    }
  }

  async function deleteList() {
    if (!selectedId) return;
    setErrorDetail(null);
    setStatus(null);
    try {
      const res = await fetch(`/api/watchlists/${encodeURIComponent(selectedId)}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
        reportFailure(res, data);
        return;
      }
      setSelectedId(null);
      setQuotes(null);
      await loadLists();
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    }
  }

  async function loadQuotes() {
    if (!selectedId) return;
    setQuotesLoading(true);
    setErrorDetail(null);
    setStatus(null);
    setQuotes(null);
    try {
      const res = await fetch(`/api/watchlists/${encodeURIComponent(selectedId)}/quotes`, {
        cache: "no-store",
      });
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      if (!res.ok) {
        reportFailure(res, data);
        return;
      }
      setQuotes(data as unknown as QuotesResponse);
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setQuotesLoading(false);
    }
  }

  return (
    <Panel
      className={className}
      title="Watchlists"
      description="Symbols you are researching, priced together from the same market-data vendor a single quote lookup uses. A symbol the vendor cannot price keeps its row and says so — no stand-in price is ever shown."
      actions={
        selected ? (
          <Pill tone="neutral" testId="selected-watchlist-pill">
            <span className="text-ink-faint">viewing</span>
            <span className="max-w-[18ch] truncate">{selected.name}</span>
          </Pill>
        ) : null
      }
    >
      <form onSubmit={createList} className="flex flex-wrap items-end gap-3">
        <Field label="New watchlist name" className="min-w-[12rem] flex-1">
          <input
            className={inputClass}
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Semis"
            required
          />
        </Field>
        <button type="submit" className={btnPrimary}>
          Create watchlist
        </button>
        <button type="button" onClick={loadLists} disabled={loading} className={btnSecondary}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </form>

      {errorDetail && (
        <Alert tone="error">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </Alert>
      )}

      {lists && lists.length === 0 && (
        <EmptyNote>
          You have no watchlists yet. Create one above — nothing is created for you.
        </EmptyNote>
      )}

      {lists && lists.length > 0 && (
        <>
          <div className="flex flex-wrap items-end gap-3">
            <Field label="Watchlist" className="min-w-[12rem] flex-1">
              <select
                className={inputClass}
                value={selectedId ?? ""}
                onChange={(e) => {
                  setSelectedId(e.target.value);
                  setQuotes(null);
                }}
              >
                {lists.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name}
                  </option>
                ))}
              </select>
            </Field>
            <button
              type="button"
              onClick={loadQuotes}
              disabled={quotesLoading || !selectedId}
              className={btnPrimary}
            >
              {quotesLoading ? "Loading quotes…" : "Load quotes"}
            </button>
            <button type="button" onClick={deleteList} className={btnSecondary}>
              Delete watchlist
            </button>
          </div>

          <form onSubmit={addSymbol} className="flex flex-wrap items-end gap-3">
            <Field
              label="Add symbol"
              className="min-w-[10rem] flex-1"
              hint="Longbridge symbol format, e.g. AAPL.US. Adding one already on this list is a real 409 from the backend, not a silent no-op."
            >
              <input
                className={monoInputClass}
                value={newSymbol}
                onChange={(e) => setNewSymbol(e.target.value)}
                placeholder="AAPL.US"
                required
              />
            </Field>
            <button type="submit" className={btnSecondary}>
              Add
            </button>
          </form>

          {selected && selected.symbols.length === 0 && (
            <EmptyNote>No symbols on this watchlist yet.</EmptyNote>
          )}

          {selected && selected.symbols.length > 0 && (
            <div className="flex flex-wrap gap-2" data-testid="watchlist-symbols">
              {selected.symbols.map((s) => (
                <span
                  key={s}
                  className="inline-flex items-center gap-2 rounded-full border border-line bg-well px-2.5 py-0.5 font-mono text-[11px] text-ink"
                >
                  {s}
                  <button
                    type="button"
                    aria-label={`Remove ${s}`}
                    onClick={() => removeSymbol(s)}
                    className="cursor-pointer text-ink-faint transition-colors hover:text-neg"
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
        </>
      )}

      {quotes && (
        <div className="flex flex-col gap-2">
          {!quotes.market_data_configured && (
            <Alert tone="warn" testId="watchlist-not-configured">
              NOT_CONFIGURED: no market-data vendor is wired, so no symbol below can be
              priced. Every row shows the backend&rsquo;s own sentinel rather than a
              stand-in value.
            </Alert>
          )}

          {quotes.quotes.length === 0 ? (
            <EmptyNote>This watchlist has no symbols to price.</EmptyNote>
          ) : (
            <TableScroll>
              <table className={tableClass}>
                <thead>
                  <tr className={theadRowClass}>
                    <th className={thClass}>symbol</th>
                    <th className={thClass}>price</th>
                    <th className={thClass}>as_of</th>
                    <th className={thClass}>source</th>
                  </tr>
                </thead>
                <tbody>
                  {quotes.quotes.map((q) => (
                    <tr key={q.symbol} className={tbodyRowClass}>
                      <td className={`${tdClass} font-semibold`}>{q.symbol}</td>
                      {q.unavailable ? (
                        <td className={`${tdClass} text-neg`} colSpan={3}>
                          {q.unavailable}
                        </td>
                      ) : (
                        <>
                          <td className={`${tdClass} text-accent`}>{q.price}</td>
                          <td className={`${tdClass} text-ink-muted`}>{q.as_of}</td>
                          <td className={tdClass}>{q.source}</td>
                        </>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>
          )}
        </div>
      )}
    </Panel>
  );
}
