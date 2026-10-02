"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import { classifyWithAbsences, useKeyedFetch } from "@/lib/useKeyedFetch";
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
  hintClass,
  inputClass,
  monoInputClass,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";
import type { Bot, BotRun } from "@/components/autotrade/types";
import ScanBoard from "./ScanBoard";
import type { BotScan } from "./types";

/**
 * A crypto or a forex bot desk (Phase 107, D133): create a 24-hour bot for
 * that asset class, run it, and read its dashboard - the recommendation
 * and every setup's scan score per symbol, refreshed every minute.
 */

type AssetClass = "crypto" | "forex";
type BrokerRow = { id: string; name: string; kind: string };

const UNIVERSE: Record<AssetClass, string[]> = {
  crypto: ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD", "DOT-USD", "SUI-USD", "DOGE-USD", "LINK-USD", "AVAX-USD"],
  forex: ["EURUSD.FX", "GBPUSD.FX", "USDJPY.FX", "AUDUSD.FX", "USDCAD.FX", "USDCHF.FX", "USDSGD.FX", "USDINR.FX", "XAUUSD.FX", "XAGUSD.FX"],
};

const COPY: Record<AssetClass, { title: string; about: string; defaults: string[]; capital: string; interval: string }> = {
  crypto: {
    title: "Crypto bot",
    about:
      "Trades coins around the clock on Coinbase's free public data, sized in fractional coins. Positions are never forced flat; exits come from the stop, the trail and the take profit.",
    defaults: ["BTC-USD", "ETH-USD", "SOL-USD"],
    capital: "1000",
    interval: "5m",
  },
  forex: {
    title: "Forex bot",
    about:
      "Trades pairs and spot metals Sunday 17:00 to Friday 17:00 New York on Twelve Data mid-price bars (needs TWELVEDATA_API_KEY). Pairs refresh at most every 15 minutes to stay inside the free 800 requests a day, so 15-minute bars suit it best.",
    defaults: ["EURUSD.FX", "XAUUSD.FX"],
    capital: "10000",
    interval: "15m",
  },
};

const STATUS_TONE: Record<Bot["status"], "neutral" | "pos" | "warn" | "neg"> = {
  pending_approval: "warn",
  active: "pos",
  paused: "neutral",
  stopped: "neg",
};

async function readJson(res: Response): Promise<Record<string, unknown> | null> {
  return (await res.json().catch(() => null)) as Record<string, unknown> | null;
}

export default function AssetBotsWorkbench({ assetClass }: { assetClass: AssetClass }) {
  const copy = COPY[assetClass];
  const classifyBrokers = useMemo(
    () => classifyWithAbsences<{ brokers: BrokerRow[] }>([404], "No brokers."),
    [],
  );
  const { data: brokersPayload } = useKeyedFetch<{ brokers: BrokerRow[] }>({
    key: `asset-bot-brokers-${assetClass}`,
    url: "/api/brokers?limit=50&offset=0",
    classify: classifyBrokers,
  });
  const brokers = (brokersPayload?.brokers ?? []).filter((b) => b.kind === "paper");

  const [name, setName] = useState(copy.title);
  const [brokerId, setBrokerId] = useState("");
  const [symbols, setSymbols] = useState<string[]>(copy.defaults);
  const [custom, setCustom] = useState("");
  const [interval, setInterval_] = useState(copy.interval);
  const [capital, setCapital] = useState(copy.capital);
  const [minScore, setMinScore] = useState("4");
  const [allowShort, setAllowShort] = useState(true);
  const [mode, setMode] = useState("auto");

  const [allBots, setAllBots] = useState<Bot[] | null>(null);
  const [picked, setSelected] = useState<string | null>(null);
  const [scan, setScan] = useState<BotScan | null>(null);
  const [scanError, setScanError] = useState<string | null>(null);
  const [runs, setRuns] = useState<BotRun[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const effectiveBroker = brokerId || brokers[0]?.id || "";

  const loadBots = useCallback(async () => {
    try {
      const res = await fetch("/api/autotrade/bots", { cache: "no-store" });
      if (handleExpiredSession(res.status)) return;
      const data = (await readJson(res)) as { bots?: Bot[] } | null;
      setAllBots(data?.bots ?? []);
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    }
  }, []);

  const bots = useMemo(
    () => (allBots === null ? null : allBots.filter((b) => b.asset_class === assetClass)),
    [allBots, assetClass],
  );
  const selected = picked ?? bots?.[0]?.id ?? null;

  const loadScan = useCallback(async (id: string) => {
    try {
      const [s, r] = await Promise.all([
        fetch(`/api/autotrade/bots/${id}/scan`, { cache: "no-store" }),
        fetch(`/api/autotrade/bots/${id}/runs?limit=8`, { cache: "no-store" }),
      ]);
      if (handleExpiredSession(s.status)) return;
      const sData = await readJson(s);
      if (!s.ok) {
        setScanError((sData?.detail as string | undefined) ?? `Scan failed (HTTP ${s.status})`);
      } else {
        setScan(sData as unknown as BotScan);
        setScanError(null);
      }
      const rData = (await readJson(r)) as { runs?: BotRun[] } | null;
      setRuns(rData?.runs ?? []);
    } catch {
      setScanError("DATA_UNAVAILABLE: could not reach the trading API");
    }
  }, []);

  useEffect(() => {
    void Promise.resolve().then(loadBots);
  }, [loadBots]);

  useEffect(() => {
    if (!selected) return;
    void Promise.resolve().then(() => loadScan(selected));
    const t = window.setInterval(() => void loadScan(selected), 60_000);
    return () => window.clearInterval(t);
  }, [selected, loadScan]);

  function toggle(sym: string) {
    setSymbols((cur) => (cur.includes(sym) ? cur.filter((s) => s !== sym) : [...cur, sym]));
  }

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await fetch("/api/autotrade/bots", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          broker_id: effectiveBroker,
          symbols,
          market_type: "24h",
          bar_interval: interval,
          max_trades_per_session: 2,
          max_trades_per_day: 6,
          capital_per_trade: capital,
          strategy_mode: mode,
          setups: [],
          min_score: Number(minScore),
          allow_short: allowShort,
          stop_loss_mode: "auto",
          take_profit_mode: "auto",
          news_blackout_minutes: 0,
        }),
      });
      const data = await readJson(res);
      if (handleExpiredSession(res.status)) return;
      if (!res.ok) {
        setError((data?.detail as string | undefined) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setNotice(`Created ${data?.name as string}. Approve it below to start trading on paper.`);
      setSelected(data?.id as string);
      await loadBots();
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setBusy(false);
    }
  }

  async function act(id: string, action: "approve" | "pause" | "resume" | "stop" | "run") {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await fetch(`/api/autotrade/bots/${id}/${action}`, { method: "POST" });
      const data = await readJson(res);
      if (handleExpiredSession(res.status)) return;
      if (!res.ok) {
        setError((data?.detail as string | undefined) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      if (action === "run") {
        setNotice(
          `Cycle ${String(data?.status)} · scanned ${String(data?.symbols_scanned ?? 0)}, signals ${String(data?.signals_found ?? 0)}, opened ${String(data?.trades_opened ?? 0)}` +
            (data?.run_detail ? ` — ${String(data.run_detail)}` : ""),
        );
      }
      await loadBots();
      await loadScan(id);
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setBusy(false);
    }
  }

  const current = bots?.find((b) => b.id === selected) ?? null;
  const buys = scan?.boards.filter((b) => b.recommendation === "BUY").length ?? 0;
  const sells = scan?.boards.filter((b) => b.recommendation === "SELL").length ?? 0;

  return (
    <div className="flex flex-col gap-5">
      <Panel title={`New ${copy.title.toLowerCase()}`} description={copy.about}>
        {error ? <Alert>{error}</Alert> : null}
        {notice ? <Alert tone="info" role="status">{notice}</Alert> : null}
        <form onSubmit={create} className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <Field label="Name">
            <input id={`${assetClass}-name`} value={name} onChange={(e) => setName(e.target.value)} className={inputClass} required />
          </Field>
          <Field label="Paper broker" hint="Bots trade paper brokers only.">
            <select id={`${assetClass}-broker`} value={effectiveBroker} onChange={(e) => setBrokerId(e.target.value)} className={inputClass}>
              {brokers.map((b) => (
                <option key={b.id} value={b.id}>{b.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Bar interval" hint={assetClass === "forex" ? "15m recommended on the free data plan." : "5m is the interval the setups were researched on."}>
            <select id={`${assetClass}-interval`} value={interval} onChange={(e) => setInterval_(e.target.value)} className={inputClass}>
              {["5m", "15m", "1h"].map((v) => (
                <option key={v} value={v}>{v}</option>
              ))}
            </select>
          </Field>
          <Field label="Capital per trade" hint={assetClass === "crypto" ? "USD per position; buys fractional coins. Keep it under 10% of the paper account." : "USD notional per position, in whole units of the base currency."}>
            <input id={`${assetClass}-capital`} type="number" min={1} step="any" value={capital} onChange={(e) => setCapital(e.target.value)} className={monoInputClass} />
          </Field>
          <Field label="Minimum scan score" hint="A setup must score at least this (0–10) to trade.">
            <input id={`${assetClass}-minscore`} type="number" min={0} max={10} value={minScore} onChange={(e) => setMinScore(e.target.value)} className={monoInputClass} />
          </Field>
          <Field label="Strategy" hint="Auto runs every setup and demotes the ones losing on this bot's own trades.">
            <select id={`${assetClass}-mode`} value={mode} onChange={(e) => setMode(e.target.value)} className={inputClass}>
              <option value="auto">Auto (all setups, self-demoting)</option>
            </select>
          </Field>
          <div className="flex flex-col gap-2 sm:col-span-2 lg:col-span-3">
            <span className="text-xs font-medium text-ink-muted">Symbols</span>
            <div className="flex flex-wrap gap-2">
              {[...new Set([...UNIVERSE[assetClass], ...symbols])].map((sym) => (
                <label key={sym} className={`inline-flex cursor-pointer items-center gap-1.5 rounded-full border px-2.5 py-1 font-mono text-xs ${symbols.includes(sym) ? "border-accent bg-well text-ink" : "border-line text-ink-muted"}`}>
                  <input type="checkbox" className="sr-only" checked={symbols.includes(sym)} onChange={() => toggle(sym)} />
                  {sym}
                </label>
              ))}
            </div>
            <div className="flex gap-2">
              <input
                id={`${assetClass}-custom`}
                placeholder={assetClass === "crypto" ? "Add e.g. AAVE-USD" : "Add e.g. EURJPY.FX"}
                value={custom}
                onChange={(e) => setCustom(e.target.value.toUpperCase())}
                className={`${monoInputClass} max-w-xs`}
              />
              <button type="button" className={btnSecondary} onClick={() => { if (custom.trim()) { toggle(custom.trim()); setCustom(""); } }}>
                Add
              </button>
            </div>
          </div>
          <label className="flex items-center gap-2 text-sm text-ink sm:col-span-2">
            <input id={`${assetClass}-short`} type="checkbox" checked={allowShort} onChange={(e) => setAllowShort(e.target.checked)} />
            Allow short positions
          </label>
          <div className="sm:col-span-2 lg:col-span-3">
            <button type="submit" className={btnPrimary} disabled={busy || !effectiveBroker || symbols.length === 0}>
              Create {copy.title.toLowerCase()}
            </button>
            {!effectiveBroker ? <p className={hintClass}>No paper broker yet. Create and approve one under Administration → Broker accounts.</p> : null}
          </div>
        </form>
      </Panel>

      <Panel title={`Your ${copy.title.toLowerCase()}s`} description="A bot trades only after you approve it. Pause steps it out; stop is final and sells nothing.">
        {bots === null ? (
          <p className="text-sm text-ink-faint">Loading…</p>
        ) : bots.length === 0 ? (
          <EmptyNote>No {copy.title.toLowerCase()}s yet. Create one above.</EmptyNote>
        ) : (
          <TableScroll>
            <table className={tableClass}>
              <thead>
                <tr className={theadRowClass}>
                  <th className={thClass}>name</th>
                  <th className={thClass}>status</th>
                  <th className={thClass}>symbols</th>
                  <th className={thClass}>interval</th>
                  <th className={thClass}>min score</th>
                  <th className={thClass}>capital</th>
                  <th className={thClass}>last cycle</th>
                  <th className={thClass}>actions</th>
                </tr>
              </thead>
              <tbody>
                {bots.map((b) => (
                  <tr key={b.id} className={`${tbodyRowClass} ${b.id === selected ? "bg-well" : ""}`}>
                    <td className={tdClass}>
                      <button type="button" className="underline-offset-2 hover:underline" onClick={() => setSelected(b.id)}>{b.name}</button>
                    </td>
                    <td className={tdClass}><Pill tone={STATUS_TONE[b.status]}>{b.status}</Pill></td>
                    <td className={tdClass}>{b.symbols.join(", ")}</td>
                    <td className={tdClass}>{b.bar_interval}</td>
                    <td className={tdClass}>{b.min_score}</td>
                    <td className={tdClass}>{b.capital_per_trade}</td>
                    <td className={tdClass}>{b.last_evaluated_at ? new Date(b.last_evaluated_at).toLocaleTimeString() : "—"}</td>
                    <td className={`${tdClass} flex gap-1`}>
                      {b.status === "pending_approval" ? <button type="button" className={btnPrimary} disabled={busy} onClick={() => act(b.id, "approve")}>Approve</button> : null}
                      {b.status === "active" ? <button type="button" className={btnGhost} disabled={busy} onClick={() => act(b.id, "pause")}>Pause</button> : null}
                      {b.status === "paused" ? <button type="button" className={btnGhost} disabled={busy} onClick={() => act(b.id, "resume")}>Resume</button> : null}
                      {b.status !== "stopped" ? <button type="button" className={btnGhost} disabled={busy} onClick={() => act(b.id, "stop")}>Stop</button> : null}
                      <button type="button" className={btnSecondary} disabled={busy} onClick={() => act(b.id, "run")}>Scan now</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        )}
      </Panel>

      {current ? (
        <Panel
          title={`${current.name} — dashboard`}
          description="Every setup scored on each symbol's latest bar, and the recommendation it adds up to. Refreshes every minute. A score is the setup's own rating of the pattern; confidence is how often that setup, side and score won +1R before its stop on this symbol over the last 60 sessions."
        >
          {scanError ? <Alert>{scanError}</Alert> : null}
          {scan ? (
            <div className="flex flex-col gap-4">
              <div className="flex flex-wrap gap-3 text-xs">
                <span className="rounded-md bg-well px-3 py-2"><span className="text-ink-faint">symbols </span><span className="font-mono text-ink">{scan.boards.length}</span></span>
                <span className="rounded-md bg-well px-3 py-2"><span className="text-ink-faint">buy </span><span className="font-mono text-pos">{buys}</span></span>
                <span className="rounded-md bg-well px-3 py-2"><span className="text-ink-faint">sell </span><span className="font-mono text-neg">{sells}</span></span>
                <span className="rounded-md bg-well px-3 py-2"><span className="text-ink-faint">wait </span><span className="font-mono text-ink">{scan.boards.length - buys - sells}</span></span>
                <span className="rounded-md bg-well px-3 py-2"><span className="text-ink-faint">sides </span><span className="font-mono text-ink">{scan.directions.join(" + ")}</span></span>
                <span className="rounded-md bg-well px-3 py-2"><span className="text-ink-faint">scanned </span><span className="font-mono text-ink">{new Date(scan.generated_at).toLocaleTimeString()}</span></span>
              </div>
              {scan.evidence.length ? (
                <div className="rounded-md border-l-2 border-accent bg-well px-3 py-2 text-xs text-ink-muted">
                  <span className="font-semibold text-ink">What the research measured: </span>
                  {scan.evidence.join(" ")}
                </div>
              ) : null}
              {scan.boards.map((board) => (
                <ScanBoard key={board.symbol} board={board} minScore={scan.min_score} />
              ))}
            </div>
          ) : !scanError ? (
            <p className="text-sm text-ink-faint">Scanning…</p>
          ) : null}

          {runs && runs.length ? (
            <div className="mt-4">
              <p className="mb-2 text-xs font-medium text-ink-muted">Recent cycles</p>
              <TableScroll>
                <table className={tableClass}>
                  <thead>
                    <tr className={theadRowClass}>
                      <th className={thClass}>started</th>
                      <th className={thClass}>status</th>
                      <th className={thClass}>scanned</th>
                      <th className={thClass}>signals</th>
                      <th className={thClass}>opened</th>
                      <th className={thClass}>closed</th>
                      <th className={thClass}>detail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((r) => (
                      <tr key={r.id} className={tbodyRowClass}>
                        <td className={tdClass}>{new Date(r.started_at).toLocaleString()}</td>
                        <td className={tdClass}>{r.status}</td>
                        <td className={tdClass}>{r.symbols_scanned}</td>
                        <td className={tdClass}>{r.signals_found}</td>
                        <td className={tdClass}>{r.trades_opened}</td>
                        <td className={tdClass}>{r.trades_closed}</td>
                        <td className={`${tdClass} max-w-[36rem] truncate`} title={r.detail ?? ""}>{r.detail ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableScroll>
            </div>
          ) : null}
        </Panel>
      ) : null}
    </div>
  );
}
