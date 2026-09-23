"use client";

import { useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  EmptyNote,
  Panel,
  Pill,
  btnPrimary,
  inputClass,
} from "@/components/ui/primitives";

/**
 * The option strategy planner (Phase 91, D110).
 *
 * A form over the Phase 78 decision layer: tick the evidence that is
 * actually present, give it the regime inputs, and it either proposes a
 * defined-risk vertical or says which gate the signal failed.
 *
 * Two things this panel is careful about.
 *
 * **A refusal is a result, not an error.** It renders in the same place
 * and with the same weight as a plan, because "score 6 of 12, below the
 * 8 floor" is the answer to the question that was asked. Showing it as a
 * red failure would train someone to keep adjusting inputs until the red
 * goes away, which is how a score floor gets talked out of.
 *
 * **The modelled price is labelled on the plan itself**, not tucked into
 * a footnote. Every premium here comes from Black-Scholes on the
 * volatility typed into this form; nothing has looked at the real chain,
 * and a spread that models at 1.85 and trades at 2.10 has lost a third of
 * its edge before the first fill.
 */

type Leg = { right: string; strike: string; is_long: boolean; modeled_price: string };

type PlanResult = {
  symbol: string;
  planned: boolean;
  score: number | null;
  max_score: number;
  minimum_tradeable_score: number;
  grade: string | null;
  reason: string | null;
  path: string | null;
  dte_band: string | null;
  spread: {
    kind: string;
    long_leg: Leg;
    short_leg: Leg;
    width: string;
    net_premium: string;
    is_debit: boolean;
    max_profit: string;
    max_loss: string;
    breakeven: string;
  } | null;
  contracts: number | null;
  risk_budget: string | null;
  max_loss_total: string | null;
  max_profit_total: string | null;
  notes: Record<string, string>;
  pricing_note: string;
};

/** The §5 evidence table, with its weights shown — the weight is why a
 *  sweep and a VWAP location are not the same tick. */
const EVIDENCE: [key: string, label: string, weight: number][] = [
  ["liquidity_sweep", "Liquidity sweep", 2],
  ["market_structure_shift", "Market structure shift", 2],
  ["cross_market_confirm", "Cross-market confirm", 1],
  ["vwap_location", "VWAP location", 1],
  ["rsi_divergence", "RSI divergence", 1],
  ["volume_confirm", "Volume confirm", 1],
  ["atr_confirm", "ATR confirm", 1],
  ["major_level", "Major level", 1],
  ["option_liquidity_ok", "Option liquidity OK", 1],
  ["iv_appropriate", "IV appropriate", 1],
];

function n(v: string | null | undefined, digits = 2): string {
  if (v == null) return "—";
  const x = Number(v);
  return Number.isFinite(x) ? x.toFixed(digits) : "—";
}

export default function OptionPlanner({ symbol, spot }: { symbol: string; spot: string | null }) {
  const [bullish, setBullish] = useState(true);
  const [evidence, setEvidence] = useState<Record<string, boolean>>({});
  const [dte, setDte] = useState("3");
  const [vol, setVol] = useState("0.45");
  const [ivRank, setIvRank] = useState("0.30");
  const [equity, setEquity] = useState("100000");
  const [increment, setIncrement] = useState("1");
  const [rangeBound, setRangeBound] = useState(false);
  const [result, setResult] = useState<PlanResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const ticked = EVIDENCE.filter(([k]) => evidence[k]).reduce((a, [, , w]) => a + w, 0);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!spot) {
      setError("No live quote for this symbol, so there is no spot to plan against.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/options/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol,
          bullish,
          evidence,
          spot,
          dte_days: Number(dte),
          volatility: vol,
          iv_rank: ivRank,
          account_equity: equity,
          strike_increment: increment,
          has_directional_edge: !rangeBound,
          is_range_bound: rangeBound,
        }),
      });
      const body = await res.json().catch(() => null);
      if (handleExpiredSession(res.status)) return;
      if (!res.ok) {
        setError(
          (body?.detail as string | undefined) ?? `Request failed (HTTP ${res.status})`,
        );
        setResult(null);
        return;
      }
      setResult(body as PlanResult);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title="Strategy planner"
      description="Scores a signal against the playbook and proposes a defined-risk vertical, or says which gate it failed."
    >
      <form className="flex flex-col gap-4" onSubmit={submit}>
        <div className="flex flex-col gap-2">
          <p className="text-[11px] uppercase tracking-[0.08em] text-ink-faint">
            Evidence ({ticked} of 12 points)
          </p>
          <div className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
            {EVIDENCE.map(([key, label, weight]) => (
              <label key={key} className="flex cursor-pointer items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={!!evidence[key]}
                  onChange={(e) =>
                    setEvidence((v) => ({ ...v, [key]: e.target.checked }))
                  }
                />
                <span>{label}</span>
                <span className="font-mono text-[10px] text-ink-faint">+{weight}</span>
              </label>
            ))}
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <label className="flex flex-col gap-1 text-xs">
            Direction
            <select
              className={inputClass}
              value={bullish ? "bullish" : "bearish"}
              onChange={(e) => setBullish(e.target.value === "bullish")}
            >
              <option value="bullish">Bullish</option>
              <option value="bearish">Bearish</option>
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs">
            Days to expiry
            <input
              type="number"
              min={0}
              max={60}
              className={`${inputClass} font-mono`}
              value={dte}
              onChange={(e) => setDte(e.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            Volatility (annualised)
            <input
              type="number"
              step="0.01"
              min={0.01}
              className={`${inputClass} font-mono`}
              value={vol}
              onChange={(e) => setVol(e.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            IV rank (0–1)
            <input
              type="number"
              step="0.05"
              min={0}
              max={1}
              className={`${inputClass} font-mono`}
              value={ivRank}
              onChange={(e) => setIvRank(e.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            Account equity
            <input
              type="number"
              min={1}
              className={`${inputClass} font-mono`}
              value={equity}
              onChange={(e) => setEquity(e.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            Strike increment
            <input
              type="number"
              step="0.5"
              min={0.5}
              className={`${inputClass} font-mono`}
              value={increment}
              onChange={(e) => setIncrement(e.target.value)}
            />
          </label>
        </div>

        <label className="flex cursor-pointer items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={rangeBound}
            onChange={(e) => setRangeBound(e.target.checked)}
          />
          Range-bound (no directional edge) — routes to a credit structure instead
        </label>

        <div className="flex flex-wrap items-center gap-3">
          <button type="submit" className={btnPrimary} disabled={busy || !spot}>
            {busy ? "Planning…" : "Plan"}
          </button>
          <span className="text-[11px] text-ink-faint">
            {spot ? `spot ${n(spot)} · ${symbol}` : "no live quote for this symbol"}
          </span>
        </div>
      </form>

      {error ? <Alert>{error}</Alert> : null}

      {result ? (
        <div className="mt-4 flex flex-col gap-3 border-t border-line pt-4">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Pill tone={result.planned ? "pos" : "neutral"}>
              {result.planned ? "plan" : "no trade"}
            </Pill>
            <span className="font-mono">
              score {result.score ?? "—"}/{result.max_score} · floor{" "}
              {result.minimum_tradeable_score}
            </span>
            {result.grade ? <span className="font-mono">grade {result.grade}</span> : null}
            {result.path ? <span className="font-mono">{result.path}</span> : null}
            {result.dte_band ? <span className="font-mono">{result.dte_band}</span> : null}
          </div>

          {!result.planned ? (
            <EmptyNote>{result.reason}</EmptyNote>
          ) : result.spread ? (
            <div className="flex flex-col gap-2">
              <table className="w-full text-left text-[11px]" data-testid="plan-spread">
                <tbody className="font-mono">
                  <tr className="border-b border-line/60">
                    <td className="py-1 pr-3 font-sans text-ink-faint">structure</td>
                    <td className="py-1">{result.spread.kind}</td>
                  </tr>
                  <tr className="border-b border-line/60">
                    <td className="py-1 pr-3 font-sans text-ink-faint">long</td>
                    <td className="py-1">
                      {result.spread.long_leg.right} {n(result.spread.long_leg.strike)} @{" "}
                      {n(result.spread.long_leg.modeled_price)}
                    </td>
                  </tr>
                  <tr className="border-b border-line/60">
                    <td className="py-1 pr-3 font-sans text-ink-faint">short</td>
                    <td className="py-1">
                      {result.spread.short_leg.right} {n(result.spread.short_leg.strike)} @{" "}
                      {n(result.spread.short_leg.modeled_price)}
                    </td>
                  </tr>
                  <tr className="border-b border-line/60">
                    <td className="py-1 pr-3 font-sans text-ink-faint">
                      {result.spread.is_debit ? "debit" : "credit"}
                    </td>
                    <td className="py-1">{n(result.spread.net_premium)}</td>
                  </tr>
                  <tr className="border-b border-line/60">
                    <td className="py-1 pr-3 font-sans text-ink-faint">breakeven</td>
                    <td className="py-1">{n(result.spread.breakeven)}</td>
                  </tr>
                  <tr className="border-b border-line/60">
                    <td className="py-1 pr-3 font-sans text-ink-faint">contracts</td>
                    <td className="py-1">{result.contracts}</td>
                  </tr>
                  <tr className="border-b border-line/60">
                    <td className="py-1 pr-3 font-sans text-ink-faint">max loss</td>
                    <td className="py-1 text-neg">
                      {n(result.max_loss_total)}{" "}
                      <span className="text-ink-faint">
                        (budget {n(result.risk_budget)})
                      </span>
                    </td>
                  </tr>
                  <tr>
                    <td className="py-1 pr-3 font-sans text-ink-faint">max profit</td>
                    <td className="py-1 text-pos">{n(result.max_profit_total)}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          ) : null}

          <p className="text-[11px] leading-relaxed text-ink-faint" data-testid="pricing-note">
            {result.pricing_note}
          </p>
        </div>
      ) : null}
    </Panel>
  );
}
