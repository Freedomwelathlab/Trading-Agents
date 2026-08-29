/**
 * Shared "which broker is selected" channel (docs/DECISIONS.md D034).
 *
 * The dashboard's broker-scoped forms (trade, agent-trade, portfolio,
 * portfolio history) each own their own `brokerId` state and each sits
 * under a server component (`app/dashboard/page.tsx`), so there is no
 * common client parent to lift that state into without converting the
 * page itself into a client component. A DOM CustomEvent on `window` is
 * the smallest thing that works here and adds no dependency.
 *
 * This carries a broker id the backend already returned from
 * `GET /brokers` — it is a convenience so a user need not retype a UUID,
 * never a claim of authorization. Every form still sends the id to its
 * own route handler, and the backend still re-checks the grant on every
 * request (require_broker_access); pre-filling a field authorizes nothing.
 */

export const BROKER_SELECTED_EVENT = "trading-os:broker-selected";

export function selectBroker(brokerId: string): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(BROKER_SELECTED_EVENT, { detail: brokerId }));
}

/**
 * Subscribe `onSelect` to broker selections. Returns the unsubscribe
 * function, so callers use it directly as a `useEffect` body:
 *
 *   useEffect(() => subscribeToBrokerSelection(setBrokerId), []);
 */
export function subscribeToBrokerSelection(onSelect: (brokerId: string) => void): () => void {
  if (typeof window === "undefined") return () => {};
  const handler = (event: Event) => {
    const detail = (event as CustomEvent<unknown>).detail;
    if (typeof detail === "string" && detail) onSelect(detail);
  };
  window.addEventListener(BROKER_SELECTED_EVENT, handler);
  return () => window.removeEventListener(BROKER_SELECTED_EVENT, handler);
}
