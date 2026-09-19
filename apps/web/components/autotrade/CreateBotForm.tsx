"use client";

import { useMemo, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import { classifyWithAbsences, useKeyedFetch } from "@/lib/useKeyedFetch";
import {
  Alert,
  Field,
  Panel,
  btnPrimary,
  hintClass,
  inputClass,
  monoInputClass,
} from "@/components/ui/primitives";
import type { Bot } from "./types";

/**
 * The bot's inputs, exactly the eight the operator asked for (Phase 81):
 * symbols from a watchlist, market phase, trades in live, trades per day,
 * capital per trade, strategy selection, stop-loss + trailing stop,
 * take-profit + trailing take profit — plus the broker it trades on and
 * an optional news blackout.
 *
 * Submitting creates the bot PENDING_APPROVAL. Nothing trades until the
 * operator approves it on the list below, and even then only on a paper
 * broker.
 */

type BrokerRow = { id: string; name: string; kind: string; is_active: boolean };
type WatchlistRow = { id: string; name: string; symbols: string[] };

const MARKET_TYPES = [
  ["regular", "Regular session (09:30–16:00 ET)"],
  ["auto", "Auto — any phase with bars (04:00–20:00 ET)"],
  ["pre_market", "Pre-market only (04:00–09:30 ET)"],
  ["post_market", "Post-market only (16:00–20:00 ET)"],
] as const;

export default function CreateBotForm({ onCreated }: { onCreated: (bot: Bot) => void }) {
  const classifyBrokers = useMemo(
    () => classifyWithAbsences<{ brokers: BrokerRow[] }>([404], "No brokers."),
    [],
  );
  const { data: brokersPayload } = useKeyedFetch<{ brokers: BrokerRow[] }>({
    key: "bot-brokers",
    url: "/api/brokers?limit=50&offset=0",
    classify: classifyBrokers,
  });
  const brokers = (brokersPayload?.brokers ?? []).filter((b) => b.kind === "paper");

  const classifyLists = useMemo(
    () => classifyWithAbsences<{ watchlists: WatchlistRow[] }>([404], "No watchlists."),
    [],
  );
  const { data: listsPayload } = useKeyedFetch<{ watchlists: WatchlistRow[] }>({
    key: "bot-watchlists",
    url: "/api/watchlists?limit=50&offset=0",
    classify: classifyLists,
  });
  const watchlists = listsPayload?.watchlists ?? [];

  const classifySetups = useMemo(
    () => classifyWithAbsences<{ setups: string[] }>([404], "No setups."),
    [],
  );
  const { data: setupsPayload } = useKeyedFetch<{ setups: string[] }>({
    key: "bot-setups",
    url: "/api/autotrade/setups",
    classify: classifySetups,
  });
  const allSetups = setupsPayload?.setups ?? [];

  const [name, setName] = useState("Intraday bot");
  const [brokerId, setBrokerId] = useState("");
  const [watchlistId, setWatchlistId] = useState("");
  // Tagged with the watchlist it was picked for, so a change of list
  // derives a fresh default instead of resetting state in an effect.
  const [pickedFor, setPickedFor] = useState<{ list: string; symbols: string[] } | null>(null);
  const [marketType, setMarketType] = useState("regular");
  const [tradesLive, setTradesLive] = useState("2");
  const [tradesDay, setTradesDay] = useState("4");
  const [capital, setCapital] = useState("2500");
  const [strategyMode, setStrategyMode] = useState("auto");
  const [setups, setSetups] = useState<string[]>([]);
  const [minScore, setMinScore] = useState("3");
  const [slMode, setSlMode] = useState("auto");
  const [slMax, setSlMax] = useState("1.5");
  const [trailSl, setTrailSl] = useState("");
  const [tpMode, setTpMode] = useState("auto");
  const [tpMin, setTpMin] = useState("2");
  const [trailTp, setTrailTp] = useState("");
  const [blackout, setBlackout] = useState("0");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Defaults derived from what came back, chosen once, without an effect
  // per fetch: the first paper broker and the first watchlist.
  const effectiveBroker = brokerId || brokers[0]?.id || "";
  const effectiveWatchlist = watchlistId || watchlists[0]?.id || "";
  const listSymbols = watchlists.find((w) => w.id === effectiveWatchlist)?.symbols ?? [];
  const picked =
    pickedFor && pickedFor.list === effectiveWatchlist ? pickedFor.symbols : listSymbols;
  const setPicked = (symbols: string[]) => setPickedFor({ list: effectiveWatchlist, symbols });

  function toggle(list: string[], set: (v: string[]) => void, value: string) {
    set(list.includes(value) ? list.filter((x) => x !== value) : [...list, value]);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const payload = {
        name,
        broker_id: effectiveBroker,
        watchlist_id: effectiveWatchlist || null,
        symbols: picked,
        market_type: marketType,
        bar_interval: "5m",
        max_trades_per_session: Number(tradesLive),
        max_trades_per_day: Number(tradesDay),
        capital_per_trade: capital,
        strategy_mode: strategyMode,
        setups: strategyMode === "auto" ? [] : setups,
        min_score: Number(minScore),
        stop_loss_mode: slMode,
        stop_loss_max_pct: slMode === "max" && slMax ? slMax : null,
        trailing_stop_pct: trailSl ? trailSl : null,
        take_profit_mode: tpMode,
        take_profit_min_pct: tpMode === "min" && tpMin ? tpMin : null,
        trailing_take_profit_pct: trailTp ? trailTp : null,
        news_blackout_minutes: Number(blackout),
      };
      const res = await fetch("/api/autotrade/bots", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      if (handleExpiredSession(res.status)) return;
      if (!res.ok) {
        setError((data?.detail as string | undefined) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      onCreated(data as unknown as Bot);
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title="New bot"
      description="Fill the inputs; the bot is created pending approval and trades nothing until you approve it below. Paper brokers only."
    >
      <form onSubmit={submit} className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        <Field label="Name">
          <input id="bot-name" value={name} onChange={(e) => setName(e.target.value)} className={inputClass} required />
        </Field>
        <Field label="Broker (paper)" hint={brokers.length === 0 ? "No paper broker granted to you yet — ask an admin." : undefined}>
          <select id="bot-broker" value={effectiveBroker} onChange={(e) => setBrokerId(e.target.value)} className={inputClass}>
            {brokers.map((b) => (
              <option key={b.id} value={b.id}>{b.name}</option>
            ))}
          </select>
        </Field>
        <Field label="Market" hint="Which session phase the bot may open and manage positions in. It is always flat by the end of that phase.">
          <select id="bot-market" value={marketType} onChange={(e) => setMarketType(e.target.value)} className={inputClass}>
            {MARKET_TYPES.map(([v, l]) => (
              <option key={v} value={v}>{l}</option>
            ))}
          </select>
        </Field>

        <Field label="Watchlist" className="md:col-span-2 xl:col-span-3" hint={watchlists.length === 0 ? "No watchlist yet — create one in the left rail." : "Tick the tickers the bot scans. It ranks every signal across all of them and takes the best."}>
          <div className="flex flex-col gap-2">
            <select id="bot-watchlist" value={effectiveWatchlist} onChange={(e) => setWatchlistId(e.target.value)} className={inputClass}>
              {watchlists.map((w) => (
                <option key={w.id} value={w.id}>{w.name} ({w.symbols.length})</option>
              ))}
            </select>
            <div className="flex flex-wrap gap-2">
              {listSymbols.map((s) => (
                <label key={s} className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-line px-2 py-1 font-mono text-xs">
                  <input type="checkbox" checked={picked.includes(s)} onChange={() => toggle(picked, setPicked, s)} />
                  {s}
                </label>
              ))}
            </div>
          </div>
        </Field>

        <Field label="Trades in live (open at once)">
          <input id="bot-trades-live" type="number" min={1} max={50} value={tradesLive} onChange={(e) => setTradesLive(e.target.value)} className={monoInputClass} />
        </Field>
        <Field label="Trades per day">
          <input id="bot-trades-day" type="number" min={1} max={200} value={tradesDay} onChange={(e) => setTradesDay(e.target.value)} className={monoInputClass} />
        </Field>
        <Field label="Capital per trade" hint="Sized to the most whole shares this buys at the signal price; the risk engine may trim it.">
          <input id="bot-capital" type="number" min={1} step="any" value={capital} onChange={(e) => setCapital(e.target.value)} className={monoInputClass} />
        </Field>

        <Field label="Strategy" className="md:col-span-2 xl:col-span-3" hint="Auto runs every setup and demotes the ones that measure negative on this bot's own closed trades (20+). Single / multi run exactly what you tick.">
          <div className="flex flex-col gap-2">
            <select id="bot-strategy-mode" value={strategyMode} onChange={(e) => setStrategyMode(e.target.value)} className={inputClass}>
              <option value="auto">Auto</option>
              <option value="single">Single</option>
              <option value="multi">Multiple</option>
            </select>
            {strategyMode !== "auto" ? (
              <div className="flex flex-wrap gap-2">
                {allSetups.map((s) => (
                  <label key={s} className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-line px-2 py-1 font-mono text-xs">
                    <input
                      type={strategyMode === "single" ? "radio" : "checkbox"}
                      name="bot-setups"
                      checked={setups.includes(s)}
                      onChange={() => strategyMode === "single" ? setSetups([s]) : toggle(setups, setSetups, s)}
                    />
                    {s}
                  </label>
                ))}
              </div>
            ) : null}
          </div>
        </Field>
        <Field label="Minimum signal score" hint="Each setup scores its evidence 0–5. Below this, the signal is skipped.">
          <input id="bot-min-score" type="number" min={0} max={10} value={minScore} onChange={(e) => setMinScore(e.target.value)} className={monoInputClass} />
        </Field>

        <Field label="Stop loss" hint="Auto = the setup's own structural stop. Max = the tighter of that and this % below entry.">
          <div className="flex gap-2">
            <select id="bot-sl-mode" value={slMode} onChange={(e) => setSlMode(e.target.value)} className={inputClass}>
              <option value="auto">Auto</option>
              <option value="max">Max %</option>
            </select>
            <input id="bot-sl-max" type="number" step="any" min={0} placeholder="%" disabled={slMode !== "max"} value={slMax} onChange={(e) => setSlMax(e.target.value)} className={monoInputClass} />
          </div>
        </Field>
        <Field label="Trailing stop loss %" hint="Optional. Ratchets the stop up to this % below the highest price seen; never down.">
          <input id="bot-trail-sl" type="number" step="any" min={0} placeholder="off" value={trailSl} onChange={(e) => setTrailSl(e.target.value)} className={monoInputClass} />
        </Field>
        <Field label="Take profit" hint="Auto = 2R from entry. Min = at least this % above entry.">
          <div className="flex gap-2">
            <select id="bot-tp-mode" value={tpMode} onChange={(e) => setTpMode(e.target.value)} className={inputClass}>
              <option value="auto">Auto</option>
              <option value="min">Min %</option>
            </select>
            <input id="bot-tp-min" type="number" step="any" min={0} placeholder="%" disabled={tpMode !== "min"} value={tpMin} onChange={(e) => setTpMin(e.target.value)} className={monoInputClass} />
          </div>
        </Field>
        <Field label="Trailing take profit %" hint="Optional. Once the target is reached, keep running and exit after this % giveback from the peak.">
          <input id="bot-trail-tp" type="number" step="any" min={0} placeholder="off" value={trailTp} onChange={(e) => setTrailTp(e.target.value)} className={monoInputClass} />
        </Field>
        <Field label="News blackout (minutes)" hint="No new entry on a symbol with a headline inside this window. 0 = off.">
          <input id="bot-blackout" type="number" min={0} max={1440} value={blackout} onChange={(e) => setBlackout(e.target.value)} className={monoInputClass} />
        </Field>

        <div className="flex items-end md:col-span-2 xl:col-span-3">
          <button type="submit" className={btnPrimary} disabled={busy || !effectiveBroker || picked.length === 0}>
            {busy ? "Creating…" : "Create bot (pending approval)"}
          </button>
        </div>
        {error ? (
          <div className="md:col-span-2 xl:col-span-3"><Alert>{error}</Alert></div>
        ) : null}
        <p className={`${hintClass} md:col-span-2 xl:col-span-3`}>
          Long only. Every entry and exit goes through the same risk engine as a manual trade; the bot cannot bypass it.
        </p>
      </form>
    </Panel>
  );
}
