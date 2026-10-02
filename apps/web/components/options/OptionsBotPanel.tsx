"use client";

import OptionsScanDashboard from "@/components/bots/OptionsScanDashboard";
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

/**
 * The options paper bot (Phase 102, D122): create one, approve / pause /
 * stop it, and read what it did.
 *
 * Paper brokers only — the broker list is filtered to `kind === "paper"`
 * and the backend refuses a live one anyway. A bot is created PENDING
 * APPROVAL and does nothing until approved. Every price it trades at is a
 * modelled fill from a ~15-minute-DELAYED chain, and the panel says so.
 */

export type OptionsBot = {
  id: string;
  name: string;
  broker_id: string;
  status: "pending_approval" | "active" | "paused" | "stopped";
  underlying: string;
  structure_type: string;
  target_delta: string;
  dte_min: number;
  dte_max: number;
  spread_width: string | null;
  profit_target_pct: string;
  stop_pct: string;
  max_concurrent_positions: number;
  capital_per_trade: string;
  last_evaluated_at: string | null;
};

export type OptionsBotTrade = {
  id: string;
  structure_type: string;
  underlying: string;
  expiry: string;
  quantity: number;
  entry_delta: string | null;
  entry_net_price: string;
  max_loss: string;
  opened_at: string;
  closed_at: string | null;
  exit_reason: string | null;
  realized_pnl: string | null;
  return_on_risk: string | null;
};

type StatsRow = {
  key: string;
  trades: number;
  win_rate: string | null;
  expectancy: string | null;
  expectancy_on_risk: string | null;
  total_pnl: string;
};

type Stats = { closed_trades: number; total_pnl: string; by_structure: StatsRow[] };
type BrokerRow = { id: string; name: string; kind: string };

const STRUCTURES = [
  ["bull_put", "Bull put (credit)"],
  ["signal", "Signal-driven: scan picks bull put / bear call"],
  ["bear_call", "Bear call (credit)"],
  ["iron_condor", "Iron condor (credit)"],
  ["bull_call", "Bull call (debit)"],
  ["bear_put", "Bear put (debit)"],
  ["long_call", "Long call"],
  ["long_put", "Long put"],
] as const;

const SINGLE_LEG = new Set(["long_call", "long_put"]);

const STATUS_TONE: Record<OptionsBot["status"], "neutral" | "pos" | "warn" | "neg"> = {
  pending_approval: "warn",
  active: "pos",
  paused: "neutral",
  stopped: "neg",
};

async function readJson(res: Response): Promise<Record<string, unknown> | null> {
  return (await res.json().catch(() => null)) as Record<string, unknown> | null;
}

export default function OptionsBotPanel({
  underlying,
  defaultStructure = "bull_put",
  showDashboard = false,
}: {
  underlying: string;
  defaultStructure?: string;
  /** Phase 107: render the selected bot's scan dashboard (the Bots page). */
  showDashboard?: boolean;
}) {
  const classifyBrokers = useMemo(
    () => classifyWithAbsences<{ brokers: BrokerRow[] }>([404], "No brokers."),
    [],
  );
  const { data: brokersPayload } = useKeyedFetch<{ brokers: BrokerRow[] }>({
    key: "options-bot-brokers",
    url: "/api/brokers?limit=50&offset=0",
    classify: classifyBrokers,
  });
  const brokers = (brokersPayload?.brokers ?? []).filter((b) => b.kind === "paper");

  const [name, setName] = useState("Options bot");
  const [brokerId, setBrokerId] = useState("");
  const [symbol, setSymbol] = useState(underlying);
  const [structure, setStructure] = useState(defaultStructure);
  const [minSignal, setMinSignal] = useState("5");
  const [delta, setDelta] = useState("0.30");
  const [dteMin, setDteMin] = useState("3");
  const [dteMax, setDteMax] = useState("14");
  const [width, setWidth] = useState("2");
  const [target, setTarget] = useState("50");
  const [stop, setStop] = useState("100");
  const [maxOpen, setMaxOpen] = useState("2");
  const [capital, setCapital] = useState("500");

  const [bots, setBots] = useState<OptionsBot[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [trades, setTrades] = useState<OptionsBotTrade[] | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const effectiveBroker = brokerId || brokers[0]?.id || "";

  const loadBots = useCallback(async () => {
    try {
      const res = await fetch("/api/options-bots", { cache: "no-store" });
      const data = await readJson(res);
      if (handleExpiredSession(res.status)) return;
      if (!res.ok) {
        setError((data?.detail as string | undefined) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setBots((data?.bots as OptionsBot[] | undefined) ?? []);
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    }
  }, []);

  const loadDetail = useCallback(async (id: string) => {
    const get = async (path: string) => {
      const res = await fetch(`/api/options-bots/${id}/${path}`, { cache: "no-store" });
      if (handleExpiredSession(res.status) || !res.ok) return null;
      return readJson(res);
    };
    const [t, s] = await Promise.all([get("trades?limit=50"), get("stats")]);
    setTrades((t?.trades as OptionsBotTrade[] | undefined) ?? []);
    setStats((s as unknown as Stats | null) ?? null);
  }, []);

  useEffect(() => {
    void Promise.resolve().then(loadBots);
  }, [loadBots]);

  useEffect(() => {
    if (!selected) return;
    void Promise.resolve().then(() => loadDetail(selected));
  }, [selected, loadDetail]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      const res = await fetch("/api/options-bots", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          broker_id: effectiveBroker,
          underlying: symbol,
          structure_type: structure,
          target_delta: delta,
          dte_min: Number(dteMin),
          dte_max: Number(dteMax),
          spread_width: SINGLE_LEG.has(structure) ? null : width,
          profit_target_pct: target,
          stop_pct: stop,
          max_concurrent_positions: Number(maxOpen),
          capital_per_trade: capital,
          min_signal_score: structure === "signal" ? Number(minSignal) : null,
        }),
      });
      const data = await readJson(res);
      if (handleExpiredSession(res.status)) return;
      if (!res.ok) {
        setError((data?.detail as string | undefined) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setNotice("Bot created — pending approval. It trades nothing until approved.");
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
      const res = await fetch(`/api/options-bots/${id}/${action}`, { method: "POST" });
      const data = await readJson(res);
      if (handleExpiredSession(res.status)) return;
      if (!res.ok) {
        setError((data?.detail as string | undefined) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      if (action === "run" && data) {
        setNotice(
          `Cycle: ${String(data.status)}${data.run_status ? ` · run ${String(data.run_status)}` : ""}` +
            ` · opened ${String(data.trades_opened)}, closed ${String(data.trades_closed)}` +
            (data.run_detail ? ` — ${String(data.run_detail)}` : ""),
        );
      }
      await loadBots();
      if (selected) await loadDetail(selected);
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setBusy(false);
    }
  }

  const current = bots?.find((b) => b.id === selected) ?? null;

  return (
    <div className="flex flex-col gap-5">
      <Panel
        title="Options bot (paper)"
        description="Opens one defined-risk structure per session day on the underlying — strikes by the vendor's delta, expiry from your DTE band, sized to capital per trade of max loss — and closes it at your profit target or stop, or lets it settle at expiry. Paper brokers only; every fill is modelled from a ~15-minute-delayed chain and passes the Risk Engine."
      >
        <form onSubmit={create} className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
          <Field label="Name">
            <input id="ob-name" value={name} onChange={(e) => setName(e.target.value)} className={inputClass} required />
          </Field>
          <Field label="Broker (paper)" hint={brokers.length === 0 ? "No paper broker granted to you yet — ask an admin." : undefined}>
            <select id="ob-broker" value={effectiveBroker} onChange={(e) => setBrokerId(e.target.value)} className={inputClass}>
              {brokers.map((b) => (
                <option key={b.id} value={b.id}>{b.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Underlying">
            <input id="ob-underlying" value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} className={monoInputClass} required />
          </Field>
          <Field label="Structure">
            <select id="ob-structure" value={structure} onChange={(e) => setStructure(e.target.value)} className={inputClass}>
              {STRUCTURES.map(([v, l]) => (
                <option key={v} value={v}>{l}</option>
              ))}
            </select>
          </Field>
          {structure === "signal" ? (
            <Field label="Minimum signal score" hint="The underlying's best setup must score at least this (0–10) before a spread is opened on its side.">
              <input id="ob-minsignal" type="number" min={0} max={10} value={minSignal} onChange={(e) => setMinSignal(e.target.value)} className={monoInputClass} />
            </Field>
          ) : null}
          <Field label="Target delta" hint="|delta| of the bought leg (debit) or the sold leg(s) (credit), from the vendor's Greeks.">
            <input id="ob-delta" type="number" step="0.01" min={0.01} max={0.99} value={delta} onChange={(e) => setDelta(e.target.value)} className={monoInputClass} />
          </Field>
          <Field label="DTE band (min – max)">
            <div className="flex gap-2">
              <input id="ob-dte-min" type="number" min={0} max={365} value={dteMin} onChange={(e) => setDteMin(e.target.value)} className={monoInputClass} />
              <input id="ob-dte-max" type="number" min={0} max={365} value={dteMax} onChange={(e) => setDteMax(e.target.value)} className={monoInputClass} />
            </div>
          </Field>
          <Field label="Spread width" hint="Strike distance to the wing. Not used by a single option.">
            <input id="ob-width" type="number" step="any" min={0} disabled={SINGLE_LEG.has(structure)} value={width} onChange={(e) => setWidth(e.target.value)} className={monoInputClass} />
          </Field>
          <Field label="Capital per trade" hint="Max loss committed per structure; contracts = floor(capital ÷ max loss).">
            <input id="ob-capital" type="number" step="any" min={1} value={capital} onChange={(e) => setCapital(e.target.value)} className={monoInputClass} />
          </Field>
          <Field label="Profit target % of premium">
            <input id="ob-target" type="number" step="any" min={1} value={target} onChange={(e) => setTarget(e.target.value)} className={monoInputClass} />
          </Field>
          <Field label="Stop % of premium">
            <input id="ob-stop" type="number" step="any" min={1} value={stop} onChange={(e) => setStop(e.target.value)} className={monoInputClass} />
          </Field>
          <Field label="Max concurrent positions">
            <input id="ob-max-open" type="number" min={1} max={20} value={maxOpen} onChange={(e) => setMaxOpen(e.target.value)} className={monoInputClass} />
          </Field>
          <div className="flex items-end">
            <button type="submit" className={btnPrimary} disabled={busy || !effectiveBroker}>
              {busy ? "Working…" : "Create bot (pending approval)"}
            </button>
          </div>
        </form>
        {error ? <Alert>{error}</Alert> : null}
        {notice ? <Alert tone="info" role="status">{notice}</Alert> : null}

        {bots === null ? (
          <p className="text-sm text-ink-faint">Loading…</p>
        ) : bots.length === 0 ? (
          <EmptyNote>No options bots yet.</EmptyNote>
        ) : (
          <TableScroll>
            <table className={tableClass}>
              <thead>
                <tr className={theadRowClass}>
                  <th className={thClass}>name</th>
                  <th className={thClass}>status</th>
                  <th className={thClass}>underlying</th>
                  <th className={thClass}>structure</th>
                  <th className={thClass}>delta</th>
                  <th className={thClass}>DTE</th>
                  <th className={thClass}>target / stop %</th>
                  <th className={thClass}>max open</th>
                  <th className={thClass}>capital</th>
                  <th className={thClass}>actions</th>
                </tr>
              </thead>
              <tbody>
                {bots.map((b) => (
                  <tr key={b.id} className={`${tbodyRowClass} ${b.id === selected ? "bg-well" : ""}`}>
                    <td className={tdClass}>
                      <button type="button" className="underline-offset-2 hover:underline" onClick={() => setSelected(b.id)}>
                        {b.name}
                      </button>
                    </td>
                    <td className={tdClass}><Pill tone={STATUS_TONE[b.status]}>{b.status}</Pill></td>
                    <td className={tdClass}>{b.underlying}</td>
                    <td className={tdClass}>{b.structure_type}{b.spread_width ? ` (${b.spread_width} wide)` : ""}</td>
                    <td className={tdClass}>{b.target_delta}</td>
                    <td className={tdClass}>{b.dte_min}–{b.dte_max}</td>
                    <td className={tdClass}>{b.profit_target_pct} / {b.stop_pct}</td>
                    <td className={tdClass}>{b.max_concurrent_positions}</td>
                    <td className={tdClass}>{b.capital_per_trade}</td>
                    <td className={`${tdClass} flex gap-1`}>
                      {b.status === "pending_approval" ? (
                        <button type="button" className={btnPrimary} disabled={busy} onClick={() => act(b.id, "approve")}>Approve</button>
                      ) : null}
                      {b.status === "active" ? (
                        <button type="button" className={btnGhost} disabled={busy} onClick={() => act(b.id, "pause")}>Pause</button>
                      ) : null}
                      {b.status === "paused" ? (
                        <button type="button" className={btnGhost} disabled={busy} onClick={() => act(b.id, "resume")}>Resume</button>
                      ) : null}
                      {b.status !== "stopped" ? (
                        <button type="button" className={btnGhost} disabled={busy} onClick={() => act(b.id, "stop")}>Stop</button>
                      ) : null}
                      <button type="button" className={btnSecondary} disabled={busy} onClick={() => act(b.id, "run")} title="Run one cycle now">Run now</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        )}
        <p className={hintClass}>
          Stop is final but closes nothing — open structures stay on the paper book, can be closed by hand, and settle at intrinsic from the underlying&rsquo;s close at expiry.
        </p>
      </Panel>

      {current && showDashboard ? (
        <OptionsScanDashboard botId={current.id} botName={current.name} />
      ) : null}

      {current ? (
        <Panel
          title={`${current.name} — trades`}
          description="Every structure this bot opened. Entry and exit prices are per share, signed: + a debit paid, − a credit received. Return on risk = P&L ÷ max loss."
        >
          {stats ? (
            <div className="flex flex-col gap-2">
              <p className="text-xs text-ink-muted">
                Closed trades {stats.closed_trades} · P&amp;L <span className="font-mono">{stats.total_pnl}</span>
              </p>
              {stats.by_structure.map((s) => (
                <p key={s.key} className="text-xs text-ink-muted" data-testid="ob-stats-row">
                  <span className="font-mono">{s.key}</span>: {s.trades} trades · win{" "}
                  {s.win_rate !== null ? `${(Number(s.win_rate) * 100).toFixed(0)}%` : "—"} · expectancy{" "}
                  <span className="font-mono">{s.expectancy ?? "—"}</span> · on risk{" "}
                  <span className="font-mono">{s.expectancy_on_risk ?? "—"}</span>
                </p>
              ))}
            </div>
          ) : null}
          {trades === null ? (
            <p className="text-sm text-ink-faint">Loading…</p>
          ) : trades.length === 0 ? (
            <EmptyNote>No trades yet.</EmptyNote>
          ) : (
            <TableScroll>
              <table className={tableClass}>
                <thead>
                  <tr className={theadRowClass}>
                    <th className={thClass}>opened</th>
                    <th className={thClass}>structure</th>
                    <th className={thClass}>expiry</th>
                    <th className={thClass}>qty</th>
                    <th className={thClass}>delta</th>
                    <th className={thClass}>entry</th>
                    <th className={thClass}>max loss</th>
                    <th className={thClass}>exit</th>
                    <th className={thClass}>P&amp;L</th>
                    <th className={thClass}>on risk</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t) => (
                    <tr key={t.id} className={tbodyRowClass}>
                      <td className={tdClass}>{new Date(t.opened_at).toLocaleString()}</td>
                      <td className={tdClass}>{t.structure_type}</td>
                      <td className={tdClass}>{t.expiry}</td>
                      <td className={tdClass}>{t.quantity}</td>
                      <td className={tdClass}>{t.entry_delta ?? "—"}</td>
                      <td className={tdClass}>{t.entry_net_price}</td>
                      <td className={tdClass}>{t.max_loss}</td>
                      <td className={tdClass}>{t.closed_at ? t.exit_reason ?? "closed" : "open"}</td>
                      <td className={tdClass}>{t.realized_pnl ?? "—"}</td>
                      <td className={tdClass}>{t.return_on_risk ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>
          )}
        </Panel>
      ) : null}
    </div>
  );
}
