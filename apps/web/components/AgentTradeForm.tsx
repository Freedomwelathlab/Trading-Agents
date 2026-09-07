"use client";

import { Fragment, useEffect, useState } from "react";
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
  monoInputClass,
  inputClass,
} from "@/components/ui/primitives";

/**
 * The stance/summary/confidence contract every analyst read shares
 * (`AnalystReadOut` on the backend). `confidence` is a string because the
 * API serialises every Decimal as one, exactly like `fill_price`.
 */
type AnalystRead = {
  stance?: string;
  summary?: string;
  confidence?: string;
};

/** Phase 52: `TechnicalAnalystReadOut`. `indicator_context` is the verbatim
 * deterministically-computed SMA/RSI string the analyst was handed — null
 * when no history provider was configured or the series was too short. */
type TechnicalAnalystRead = AnalystRead & { indicator_context?: string | null };

/** Phase 52: `FundamentalAnalystReadOut` — provenance of the figures the
 * read narrates, straight off the vendor's own `CompanyFundamentals`. */
type FundamentalAnalystRead = AnalystRead & {
  data_source?: string;
  fundamentals_as_of?: string | null;
};

/** Phase 52: `NewsAnalystReadOut`. `headline_count` is the exact number of
 * real headlines the read is based on, counted in code, never by the model. */
type NewsAnalystRead = AnalystRead & { headline_count?: number };

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

  // Phase 52. Each is independently nullable because each analyst is
  // independently optional and independently failure-isolated on the
  // server: null means THIS analyst produced no read, and says nothing
  // about the other two.
  technical_analyst?: TechnicalAnalystRead | null;
  fundamental_analyst?: FundamentalAnalystRead | null;
  news_analyst?: NewsAnalystRead | null;
};

/**
 * One analyst's real read, or an honest statement that there wasn't one.
 *
 * Three states, deliberately distinct (Phase 52):
 *
 * - `undefined` — the key is absent entirely, i.e. an older-shaped response
 *   from before this field existed. Nothing truthful can be said about that
 *   analyst, so nothing is rendered at all. Same rule `PortfolioVerdict`
 *   already applies to an older-shaped response with no `portfolio_*` fields.
 * - `null` — the server answered and this analyst produced no read. Rendered
 *   as an explicit "no read", never as a neutral stance, a zero confidence,
 *   or a synthesised "no signal" summary.
 * - an object — the analyst's real, validated output. Only the values the
 *   backend actually sent are shown; a missing sub-field is an em dash.
 */
function AnalystReadPanel({
  name,
  testId,
  read,
  extra = [],
}: {
  name: string;
  testId: string;
  read: AnalystRead | null | undefined;
  /** Analyst-specific real fields, as [label, value] pairs. */
  extra?: [string, string | null | undefined][];
}) {
  if (read === undefined) return null;

  if (read === null) {
    return (
      <div
        data-testid={testId}
        data-analyst-state="no-read"
        className="rounded-md border border-line bg-well px-3 py-2.5 text-sm text-ink-muted"
      >
        <p className="text-[11px] font-semibold uppercase tracking-[0.09em] opacity-70">
          {name}
        </p>
        <p className="mt-1.5 leading-relaxed">
          No read on this request. This analyst either isn&rsquo;t configured
          or its read was unavailable; the response doesn&rsquo;t say which,
          and nothing is assumed here. It contributed nothing to the proposal
          above.
        </p>
      </div>
    );
  }

  return (
    <div
      data-testid={testId}
      data-analyst-state="read"
      data-stance={read.stance}
      className="rounded-md border border-line bg-well px-3 py-2.5 text-sm text-ink"
    >
      <p className="text-[11px] font-semibold uppercase tracking-[0.09em] opacity-70">
        {name}
      </p>
      <dl className="mt-1.5 grid grid-cols-[auto_1fr] items-baseline gap-x-4 gap-y-1.5 sm:grid-cols-[auto_1fr_auto_1fr]">
        <VerdictTerm>stance</VerdictTerm>
        <VerdictValue>{read.stance ?? "—"}</VerdictValue>
        <VerdictTerm>confidence</VerdictTerm>
        <VerdictValue>{read.confidence ?? "—"}</VerdictValue>
        {extra.map(([label, value]) => (
          <Fragment key={label}>
            <VerdictTerm>{label}</VerdictTerm>
            <VerdictValue>{value ?? "—"}</VerdictValue>
          </Fragment>
        ))}
      </dl>
      {read.summary && <p className="mt-2 leading-relaxed">{read.summary}</p>}
    </div>
  );
}

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
    <Panel
      title="Agent trade (AI proposal)"
      description="The agent proposes side/quantity/stop from your directive; the price always comes from a live quote, never the agent. The proposal still passes through the same deterministic Risk Engine a human-submitted trade uses."
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
        <Field label="Symbol">
          <input
            className={monoInputClass}
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            required
          />
        </Field>
        <Field
          label="Directive"
          hint="Plain-language intent. The agent turns it into a side, quantity and stop; it never sets the price."
        >
          <input
            className={inputClass}
            value={directive}
            onChange={(e) => setDirective(e.target.value)}
            required
          />
        </Field>
        <Field label="Marks for other open positions (optional)">
          <input
            className={monoInputClass}
            value={marksInput}
            onChange={(e) => setMarksInput(e.target.value)}
            placeholder="TSLA.US=250, MSFT.US=410"
          />
        </Field>

        <button type="submit" disabled={loading} className={`${btnPrimary} self-start`}>
          {loading ? "Proposing…" : "Propose agent trade"}
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
              <VerdictTerm>side</VerdictTerm>
              <VerdictValue>{result.side}</VerdictValue>
              <VerdictTerm>quantity (agent proposed)</VerdictTerm>
              <VerdictValue>{result.quantity}</VerdictValue>
              <VerdictTerm>fill_quantity</VerdictTerm>
              <VerdictValue>{result.fill_quantity ?? "—"}</VerdictValue>
              <VerdictTerm>fill_price</VerdictTerm>
              <VerdictValue>{result.fill_price ?? "—"}</VerdictValue>
            </dl>
            {result.rationale && (
              <p className="mt-2 leading-relaxed">
                <span className="opacity-70">rationale: </span>
                {result.rationale}
              </p>
            )}
            {result.detail && <p className="mt-2 leading-relaxed">{result.detail}</p>}
          </div>
          <PortfolioVerdict {...result} />

          {/* Phase 52: the per-analyst reads the response now actually
              carries. Rendered only when the response has the fields at all —
              an older-shaped body says nothing about the analysts, and this
              panel never speaks for it. */}
          {(result.technical_analyst !== undefined ||
            result.fundamental_analyst !== undefined ||
            result.news_analyst !== undefined) && (
            <section data-testid="analyst-reads" className="flex flex-col gap-2">
              <p className="text-[11px] font-semibold uppercase tracking-[0.09em] text-ink-faint">
                Analyst reads
              </p>
              <p className="max-w-prose text-xs leading-relaxed text-ink-faint">
                Informational context that was appended to the agent&rsquo;s
                prompt — never authoritative, and never a price, side or
                quantity. Each analyst is independently optional, so one having
                no read says nothing about the others. What actually gated this
                trade is the deterministic Risk Engine and Portfolio Manager
                above.
              </p>
              <AnalystReadPanel
                name="Technical Analyst"
                testId="technical-analyst"
                read={result.technical_analyst}
                extra={[
                  ["indicators", result.technical_analyst?.indicator_context],
                ]}
              />
              <AnalystReadPanel
                name="Fundamental Analyst"
                testId="fundamental-analyst"
                read={result.fundamental_analyst}
                extra={[
                  ["source", result.fundamental_analyst?.data_source],
                  ["figures as of", result.fundamental_analyst?.fundamentals_as_of],
                ]}
              />
              <AnalystReadPanel
                name="News Analyst"
                testId="news-analyst"
                read={result.news_analyst}
                extra={[
                  ["headlines read", result.news_analyst?.headline_count?.toString()],
                ]}
              />
            </section>
          )}
        </>
      )}
    </Panel>
  );
}
