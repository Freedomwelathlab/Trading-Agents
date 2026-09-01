"use client";

import { useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import { subscribeToBrokerSelection } from "@/lib/brokerSelection";
import PortfolioVerdict, {
  VerdictTerm,
  VerdictValue,
  riskVerdictTone,
  type PortfolioVerdictFields,
} from "@/components/PortfolioVerdict";
import {
  Alert,
  Field,
  Panel,
  btnPrimary,
  inputClass,
  monoInputClass,
} from "@/components/ui/primitives";

type TradeResponse = PortfolioVerdictFields & {
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

  // The panel description deliberately does not name the Portfolio
  // Manager. Its verdict panel appears only when the backend actually
  // reported a decision from that gate, and D029 requires that a response
  // where it never ran mentions it nowhere on screen — standing chrome
  // naming it would read as "it looked at this and was fine".
  return (
    <Panel
      title="Submit paper trade"
      description="A human-entered proposal, checked by the deterministic Risk Engine before any order can reach the broker. Every verdict below is one the backend returned."
    >
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <Field label="Broker ID">
          <input
            className={monoInputClass}
            value={brokerId}
            onChange={(e) => setBrokerId(e.target.value)}
            placeholder="broker UUID"
            required
          />
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Symbol">
            <input
              className={monoInputClass}
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              required
            />
          </Field>
          <Field label="Side">
            <select
              className={inputClass}
              value={side}
              onChange={(e) => setSide(e.target.value as "buy" | "sell")}
            >
              <option value="buy">buy</option>
              <option value="sell">sell</option>
            </select>
          </Field>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Quantity">
            <input
              className={monoInputClass}
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              required
            />
          </Field>
          <Field label="Est. price (optional)">
            <input
              className={monoInputClass}
              value={estimatedPrice}
              onChange={(e) => setEstimatedPrice(e.target.value)}
            />
          </Field>
          <Field label="Stop price (optional)">
            <input
              className={monoInputClass}
              value={stopPrice}
              onChange={(e) => setStopPrice(e.target.value)}
            />
          </Field>
        </div>

        <button type="submit" disabled={loading} className={`${btnPrimary} self-start`}>
          {loading ? "Submitting…" : "Submit trade"}
        </button>
      </form>

      {errorDetail && (
        <Alert tone="error">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </Alert>
      )}

      {result && !errorDetail && (
        <>
          <div
            data-testid="risk-verdict"
            className={`rounded-md border px-3 py-3 text-sm ${riskVerdictTone(result)}`}
          >
            <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.09em] opacity-70">
              Risk Engine
            </p>
            <dl className="grid grid-cols-[auto_1fr] items-baseline gap-x-4 gap-y-1.5 sm:grid-cols-[auto_1fr_auto_1fr]">
              <VerdictTerm>order_id</VerdictTerm>
              <VerdictValue>{result.order_id}</VerdictValue>
              <VerdictTerm>status</VerdictTerm>
              <VerdictValue>{result.status}</VerdictValue>
              <VerdictTerm>approved (Risk Engine)</VerdictTerm>
              <VerdictValue>{String(result.approved)}</VerdictValue>
              <VerdictTerm>block_reason</VerdictTerm>
              <VerdictValue>{result.block_reason ?? "—"}</VerdictValue>
              <VerdictTerm>fill_quantity</VerdictTerm>
              <VerdictValue>{result.fill_quantity ?? "—"}</VerdictValue>
              <VerdictTerm>fill_price</VerdictTerm>
              <VerdictValue>{result.fill_price ?? "—"}</VerdictValue>
            </dl>
            {result.detail && <p className="mt-2 leading-relaxed">{result.detail}</p>}
          </div>
          <PortfolioVerdict {...result} />
        </>
      )}
    </Panel>
  );
}
