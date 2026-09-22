"use client";

import { useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import { subscribeToBrokerSelection } from "@/lib/brokerSelection";
import {
  Alert,
  EmptyNote,
  Field,
  Panel,
  Pill,
  TableScroll,
  btnGhost,
  btnPrimary,
  inputClass,
  monoInputClass,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";

type FillEntry = {
  id: string;
  order_id: string;
  quantity: string;
  fill_price: string;
  filled_at: string;
};

type OrderEntry = {
  id: string;
  broker_id: string;
  broker_kind: string;
  symbol: string;
  side: string;
  quantity: string;
  estimated_price: string;
  stop_price: string | null;
  status: string;
  risk_block_reason: string | null;
  risk_detail: string | null;
  portfolio_action: string | null;
  portfolio_binding_constraint: string | null;
  portfolio_detail: string | null;
  portfolio_requested_quantity: string | null;
  submitted_by_user_id: string | null;
  submitted_at: string;
  fills: FillEntry[];
};

type OrdersResponse = {
  orders?: OrderEntry[];
  limit?: number;
  offset?: number;
  detail?: string;
};

/**
 * The four real `OrderStatus` members (`apps/api/app/db/models.py`), each
 * with the meaning that enum's own docstrings give it.
 *
 * These are NOT collapsed into a filled/not-filled binary, and the two
 * "nothing executed" statuses are NOT merged. `rejected` means **this
 * system** stopped the trade — the Risk Engine, the emergency stop, the
 * duplicate check or the Portfolio Manager — and the order never reached a
 * venue at all. `broker_closed_unfilled` means it *did* reach a real
 * broker, which then ended it unexecuted. Showing those as one thing would
 * misreport whether the platform's own controls fired, which is the single
 * question this panel exists to answer.
 *
 * Each status therefore gets its own tone and its own sentence. An unknown
 * status string — a member added to the backend enum after this file was
 * written — is rendered verbatim with no tone and no invented meaning,
 * rather than being bucketed into whichever of these looks closest.
 */
const STATUS_MEANING: Record<
  string,
  { tone: "pos" | "neg" | "warn" | "neutral"; meaning: string }
> = {
  filled: {
    tone: "pos",
    meaning: "Executed. The executed price is on the fill, not on the order.",
  },
  rejected: {
    tone: "neg",
    meaning:
      "Blocked by THIS system — Risk Engine, emergency stop, duplicate check or Portfolio Manager. It never reached a broker.",
  },
  submitted_unconfirmed: {
    tone: "warn",
    meaning:
      "Not terminal. A real order exists at a real broker and the broker has not yet reported whether it executed.",
  },
  broker_closed_unfilled: {
    tone: "neutral",
    meaning:
      "Terminal. The BROKER ended the order having executed nothing — cancelled, expired, or rejected by the venue.",
  },
};

/**
 * Renders one order's status honestly. Unknown values keep their raw text
 * and get the neutral tone; nothing here guesses.
 */
export function StatusCell({ status }: { status: string }) {
  const known = STATUS_MEANING[status];
  return (
    <Pill tone={known?.tone ?? "neutral"} testId={`status-${status}`}>
      <span title={known?.meaning}>{status}</span>
    </Pill>
  );
}

/**
 * The price(s) actually executed for an order, or an em-dash when the
 * order has no fills.
 *
 * A rejected order, and an order the broker closed unfilled, both have
 * `fills: []` — the backend returns an empty array, never a null price or
 * a zero. This renders that as "no fill", never as `0.00` and never by
 * falling back to `estimated_price`, which is the price the proposal was
 * *evaluated* at and would be a fabricated execution if shown in this
 * column.
 */
export function fillPriceText(fills: FillEntry[]): string {
  if (!fills || fills.length === 0) return "—";
  return fills.map((f) => f.fill_price).join(", ");
}

/**
 * Broker-scoped order history (Phase 51), on top of Phase 48/D065's
 * read-only listing endpoints.
 *
 * Every column is a field the backend actually returned. Two things this
 * panel deliberately does not show, because D065's response shape does not
 * carry them: an order *type* (there is no such column — see D065), and
 * the venue's own wording for a `broker_closed_unfilled` order (that lives
 * in `orders.broker_status`, which `OrderResponse` does not expose). Both
 * are absences, and an absence is rendered as one.
 *
 * Pagination is real: Prev/Next re-request the backend with a new
 * `offset`, they do not slice a cached array. The backend returns no total
 * count, so Next is offered only while the current page came back full —
 * a short page is the only honest "there is no more" signal available, and
 * inventing a page count from one that is not returned would be worse than
 * not showing one.
 */
export default function TradeHistory({ className }: { className?: string } = {}) {
  const [brokerId, setBrokerId] = useState("");

  // D034: broker discovery can push a real, granted broker id here so the
  // user never has to paste a UUID. Pre-filling authorizes nothing — the
  // backend re-checks the grant on every request.
  useEffect(() => subscribeToBrokerSelection(setBrokerId), []);

  const [limit, setLimit] = useState("50");

  const [loading, setLoading] = useState(false);
  const [cancelling, setCancelling] = useState<string | null>(null);
  const [cancelNotice, setCancelNotice] = useState<string | null>(null);

  /** Phase 84 (D101): cancel a resting live order. The venue's answer is
   * what gets shown — a cancel can race a fill. */
  async function cancelOrder(orderId: string) {
    setCancelling(orderId);
    setCancelNotice(null);
    try {
      const res = await fetch(
        `/api/brokers/${encodeURIComponent(brokerId)}/orders/${encodeURIComponent(orderId)}/cancel`,
        { method: "POST" },
      );
      const data = (await res.json().catch(() => null)) as
        | { outcome?: string; broker_status?: string | null; detail?: string }
        | null;
      if (handleExpiredSession(res.status)) return;
      if (!res.ok) {
        setCancelNotice(`Cancel failed: ${data?.detail ?? `HTTP ${res.status}`}`);
        return;
      }
      setCancelNotice(
        `Venue answered: ${data?.outcome ?? "?"}${data?.broker_status ? ` (${data.broker_status})` : ""}`,
      );
      await load(pageLimit, pageOffset);
    } catch {
      setCancelNotice("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setCancelling(null);
    }
  }
  const [result, setResult] = useState<OrdersResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  // The limit/offset the currently-displayed page was actually fetched
  // with. Prev/Next step from these, not from the form input, so editing
  // the page-size box cannot silently re-scope the page already on screen.
  const [pageLimit, setPageLimit] = useState(50);
  const [pageOffset, setPageOffset] = useState(0);

  async function load(nextLimit: number, nextOffset: number) {
    setLoading(true);
    setErrorDetail(null);
    setStatus(null);

    const query = new URLSearchParams({
      limit: String(nextLimit),
      offset: String(nextOffset),
    });

    try {
      const res = await fetch(
        `/api/brokers/${encodeURIComponent(brokerId)}/orders?${query.toString()}`,
      );
      const data = (await res.json().catch(() => null)) as OrdersResponse | null;
      setStatus(res.status);
      if (!res.ok) {
        // A 401 means the httpOnly session cookie is gone or dead; redirect
        // to login with a real reason rather than rendering a bare HTTP 401
        // next to a table that can no longer load (D031/D032).
        if (handleExpiredSession(res.status)) return;
        // Real backend statuses only — 403 (no portfolio:view permission,
        // or no BrokerGrant for this broker), 404 (unknown broker id), 422
        // (limit/offset outside the documented bounds), 503 (the proxy
        // could not reach the API). Never a placeholder blotter.
        setResult(null);
        setErrorDetail(data?.detail ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setResult(data);
      // Trust the echoed envelope over our own request when the backend
      // sent one: it is the server's own account of which page this is.
      setPageLimit(typeof data?.limit === "number" ? data.limit : nextLimit);
      setPageOffset(typeof data?.offset === "number" ? data.offset : nextOffset);
    } catch {
      setResult(null);
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const parsed = Number(limit);
    void load(Number.isFinite(parsed) && parsed > 0 ? parsed : 50, 0);
  }

  const orders = result?.orders ?? [];
  const hasPrev = pageOffset > 0;
  // No total count is returned, so a full page is the only evidence that
  // another one may exist. A short page ends the walk.
  const hasNext = orders.length === pageLimit && orders.length > 0;

  return (
    <Panel
      className={className}
      title="Trade history"
      description="Every order this broker recorded — approved and blocked alike — newest first, straight from the append-only audit trail. Rejections are shown, not filtered: they are the part of the record that shows the controls working."
    >
      <form
        onSubmit={handleSubmit}
        className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_8rem_auto] sm:items-end"
      >
        <Field label="Broker ID">
          <input
            className={monoInputClass}
            value={brokerId}
            onChange={(e) => setBrokerId(e.target.value)}
            placeholder="broker UUID"
            required
          />
        </Field>
        <Field label="Page size (1–500)">
          <input
            type="number"
            min={1}
            max={500}
            className={inputClass}
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
          />
        </Field>
        <button type="submit" disabled={loading} className={btnPrimary}>
          {loading ? "Loading…" : "Load orders"}
        </button>
      </form>

      {errorDetail && (
        <Alert tone="error">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </Alert>
      )}
      {cancelNotice && (
        <Alert tone="info" role="status">
          {cancelNotice}
        </Alert>
      )}

      {result && !errorDetail && orders.length === 0 && (
        <EmptyNote>
          This broker has recorded no orders{pageOffset > 0 ? " on this page" : ""} — an
          empty audit trail, not a failed request.
        </EmptyNote>
      )}

      {result && !errorDetail && orders.length > 0 && (
        <TableScroll>
          <table className={tableClass}>
            <caption className="sr-only">
              Orders for this broker, exactly as returned by the API
            </caption>
            <thead>
              <tr className={theadRowClass}>
                <th className={thClass}>submitted_at</th>
                <th className={thClass}>Symbol</th>
                <th className={thClass}>Side</th>
                <th className={thClass}>Quantity</th>
                <th className={thClass}>estimated_price</th>
                <th className={thClass}>fill price</th>
                <th className={thClass}>Status</th>
                <th className={thClass}>broker_kind</th>
                <th className={thClass}>risk_block_reason</th>
                <th className={thClass}>actions</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => (
                <tr key={o.id} className={tbodyRowClass} data-testid="order-row">
                  <td className={`${tdClass} text-ink-muted`}>{o.submitted_at}</td>
                  <td className={`${tdClass} font-semibold`}>{o.symbol}</td>
                  <td className={tdClass}>{o.side}</td>
                  <td className={tdClass}>{o.quantity}</td>
                  <td className={tdClass}>{o.estimated_price}</td>
                  <td className={tdClass} data-testid={`fill-price-${o.id}`}>
                    {fillPriceText(o.fills)}
                  </td>
                  <td className={tdClass}>
                    <StatusCell status={o.status} />
                  </td>
                  <td className={tdClass}>{o.broker_kind}</td>
                  <td className={`${tdClass} text-ink-muted`}>
                    {o.risk_block_reason ? (
                      <span title={o.risk_detail ?? undefined}>{o.risk_block_reason}</span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className={tdClass}>
                    {o.status === "submitted_unconfirmed" && o.broker_kind === "live" ? (
                      <button
                        type="button"
                        className={btnGhost}
                        disabled={cancelling === o.id}
                        onClick={() => void cancelOrder(o.id)}
                        title="Ask the venue to cancel this resting order"
                      >
                        {cancelling === o.id ? "Cancelling…" : "Cancel"}
                      </button>
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}

      {/*
        The pager sits OUTSIDE the table block on purpose. Paging past the
        last order returns a real empty page, and if these controls lived
        with the rows they would disappear exactly then — stranding the
        reader one click beyond the end of the trail with no way back.
      */}
      {result && !errorDetail && (hasPrev || hasNext) && (
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            className={btnGhost}
            disabled={loading || !hasPrev}
            onClick={() => void load(pageLimit, Math.max(0, pageOffset - pageLimit))}
          >
            ← Prev
          </button>
          <button
            type="button"
            className={btnGhost}
            disabled={loading || !hasNext}
            onClick={() => void load(pageLimit, pageOffset + pageLimit)}
          >
            Next →
          </button>
          <span className="text-xs text-ink-faint" data-testid="page-range">
            Showing {orders.length} order{orders.length === 1 ? "" : "s"} at offset{" "}
            {pageOffset} (limit {pageLimit})
          </span>
        </div>
      )}
    </Panel>
  );
}
