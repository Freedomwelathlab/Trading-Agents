"use client";

import { useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import { subscribeToBrokerSelection } from "@/lib/brokerSelection";

type TradeResponse = {
  order_id?: string;
  status?: "filled" | "rejected";
  approved?: boolean;
  block_reason?: string | null;
  detail?: string | null;
  fill_quantity?: string | null;
  fill_price?: string | null;
};

export default function TradeForm() {
  const [brokerId, setBrokerId] = useState("");

  // D034: the broker discovery list can push a real, granted broker id
  // here so the user never has to paste a UUID. Pre-filling authorizes
  // nothing - the backend re-checks the grant on submit.
  useEffect(() => subscribeToBrokerSelection(setBrokerId), []);
  const [symbol, setSymbol] = useState("AAPL");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [quantity, setQuantity] = useState("10");
  const [estimatedPrice, setEstimatedPrice] = useState("");
  const [stopPrice, setStopPrice] = useState("");

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<TradeResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    const body: Record<string, unknown> = { symbol, side, quantity };
    if (estimatedPrice.trim()) body.estimated_price = estimatedPrice.trim();
    if (stopPrice.trim()) body.stop_price = stopPrice.trim();

    try {
      const res = await fetch(`/api/trades/${encodeURIComponent(brokerId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await res.json().catch(() => null)) as TradeResponse | null;
      setStatus(res.status);
      if (!res.ok) {
        // A 401 means the httpOnly session cookie is gone or dead;
        // redirect to login with a real reason rather than rendering
        // a bare HTTP 401 next to a form that can no longer work (D032).
        if (handleExpiredSession(res.status)) return;
        setErrorDetail(data?.detail ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setResult(data);
    } catch {
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="rounded border border-neutral-300 p-4 dark:border-neutral-700">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-500">
        Submit paper trade
      </h2>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Broker ID
          <input
            className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
            value={brokerId}
            onChange={(e) => setBrokerId(e.target.value)}
            placeholder="broker UUID"
            required
          />
        </label>

        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Symbol
            <input
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              required
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Side
            <select
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={side}
              onChange={(e) => setSide(e.target.value as "buy" | "sell")}
            >
              <option value="buy">buy</option>
              <option value="sell">sell</option>
            </select>
          </label>
        </div>

        <div className="grid grid-cols-3 gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Quantity
            <input
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              required
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Est. price (optional)
            <input
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={estimatedPrice}
              onChange={(e) => setEstimatedPrice(e.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Stop price (optional)
            <input
              className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
              value={stopPrice}
              onChange={(e) => setStopPrice(e.target.value)}
            />
          </label>
        </div>

        <button
          type="submit"
          disabled={loading}
          className="self-start rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
        >
          {loading ? "Submitting…" : "Submit trade"}
        </button>
      </form>

      {errorDetail && (
        <p role="alert" className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </p>
      )}

      {result && !errorDetail && (
        <div
          className={`mt-3 rounded px-3 py-2 text-sm ${
            result.status === "rejected"
              ? "bg-amber-50 text-amber-800 dark:bg-amber-950 dark:text-amber-300"
              : "bg-green-50 text-green-800 dark:bg-green-950 dark:text-green-300"
          }`}
        >
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1">
            <dt className="opacity-70">order_id</dt>
            <dd>{result.order_id}</dd>
            <dt className="opacity-70">status</dt>
            <dd>{result.status}</dd>
            <dt className="opacity-70">approved</dt>
            <dd>{String(result.approved)}</dd>
            <dt className="opacity-70">block_reason</dt>
            <dd>{result.block_reason ?? "—"}</dd>
            <dt className="opacity-70">fill_quantity</dt>
            <dd>{result.fill_quantity ?? "—"}</dd>
            <dt className="opacity-70">fill_price</dt>
            <dd>{result.fill_price ?? "—"}</dd>
          </dl>
          {result.detail && <p className="mt-2">{result.detail}</p>}
        </div>
      )}
    </section>
  );
}
