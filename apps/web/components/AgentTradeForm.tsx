"use client";

import { useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import { subscribeToBrokerSelection } from "@/lib/brokerSelection";
import PortfolioVerdict, {
  riskVerdictTone,
  type PortfolioVerdictFields,
} from "@/components/PortfolioVerdict";

type AgentTradeResponse = PortfolioVerdictFields & {
  order_id?: string;
  status?: "filled" | "rejected";
  approved?: boolean;
  block_reason?: string | null;
  detail?: string | null;
  fill_quantity?: string | null;
  fill_price?: string | null;
  side?: string;
  quantity?: string;
  rationale?: string;
};

/** Parses "SYM=price, SYM2=price2" into the marks object the API expects. */
function parseMarks(input: string): Record<string, string> {
  const marks: Record<string, string> = {};
  for (const pair of input.split(",")) {
    const [symbol, price] = pair.split("=").map((s) => s.trim());
    if (symbol && price) marks[symbol] = price;
  }
  return marks;
}

export default function AgentTradeForm() {
  const [brokerId, setBrokerId] = useState("");

  // D034: the broker discovery list can push a real, granted broker id
  // here so the user never has to paste a UUID. Pre-filling authorizes
  // nothing - the backend re-checks the grant on submit.
  useEffect(() => subscribeToBrokerSelection(setBrokerId), []);
  const [symbol, setSymbol] = useState("AAPL.US");
  const [directive, setDirective] = useState("moderate momentum long, tight stop");
  const [marksInput, setMarksInput] = useState("");

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<AgentTradeResponse | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    setErrorDetail(null);
    setStatus(null);

    const body = { symbol, directive, marks: parseMarks(marksInput) };

    try {
      const res = await fetch(`/api/agent-trades/${encodeURIComponent(brokerId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await res.json().catch(() => null)) as AgentTradeResponse | null;
      setStatus(res.status);
      if (!res.ok) {
        // A 401 means the httpOnly session cookie is gone or dead;
        // redirect to login with a real reason rather than rendering
        // a bare HTTP 401 next to a form that can no longer work (D032).
        if (handleExpiredSession(res.status)) return;
        // Real backend sentinels only — NOT_CONFIGURED: (no LLM provider or
        // market data vendor wired) and AGENT_OUTPUT_INVALID: (the agent's
        // response didn't parse) are rendered exactly as the backend sent
        // them. Never a fabricated trade result for either failure case.
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
        Agent trade (AI proposal)
      </h2>
      <p className="mb-3 text-xs text-neutral-500">
        The agent proposes side/quantity/stop from your directive; the price
        always comes from a live quote, never the agent. The proposal still
        passes through the same deterministic Risk Engine a human-submitted
        trade uses.
      </p>
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
          Directive
          <input
            className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
            value={directive}
            onChange={(e) => setDirective(e.target.value)}
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Marks for other open positions (optional)
          <input
            className="rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
            value={marksInput}
            onChange={(e) => setMarksInput(e.target.value)}
            placeholder="TSLA.US=250, MSFT.US=410"
          />
        </label>

        <button
          type="submit"
          disabled={loading}
          className="self-start rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
        >
          {loading ? "Proposing…" : "Propose agent trade"}
        </button>
      </form>

      {errorDetail && (
        <p role="alert" className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
          {status ? `HTTP ${status}: ` : ""}
          {errorDetail}
        </p>
      )}

      {result && !errorDetail && (
        <>
          <div
            data-testid="risk-verdict"
            className={`mt-3 rounded px-3 py-2 text-sm ${riskVerdictTone(result)}`}
          >
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1">
              <dt className="opacity-70">order_id</dt>
              <dd>{result.order_id}</dd>
              <dt className="opacity-70">status</dt>
              <dd>{result.status}</dd>
              <dt className="opacity-70">approved (Risk Engine)</dt>
              <dd>{String(result.approved)}</dd>
              <dt className="opacity-70">block_reason</dt>
              <dd>{result.block_reason ?? "—"}</dd>
              <dt className="opacity-70">side</dt>
              <dd>{result.side}</dd>
              <dt className="opacity-70">quantity (agent proposed)</dt>
              <dd>{result.quantity}</dd>
              <dt className="opacity-70">fill_quantity</dt>
              <dd>{result.fill_quantity ?? "—"}</dd>
              <dt className="opacity-70">fill_price</dt>
              <dd>{result.fill_price ?? "—"}</dd>
            </dl>
            {result.rationale && (
              <p className="mt-2">
                <span className="opacity-70">rationale: </span>
                {result.rationale}
              </p>
            )}
            {result.detail && <p className="mt-2">{result.detail}</p>}
          </div>
          <PortfolioVerdict {...result} />
        </>
      )}
    </section>
  );
}
