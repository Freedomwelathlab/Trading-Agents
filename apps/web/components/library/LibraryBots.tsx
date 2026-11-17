"use client";

import { useCallback, useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  EmptyNote,
  Field,
  Panel,
  Pill,
  btnGhost,
  btnPrimary,
  inputClass,
  monoInputClass,
} from "@/components/ui/primitives";
import type { LibraryBot, LibraryBotRun, LibraryStrategy } from "./types";

/**
 * Paper bots that hold what a library rule holds (Phase 109, D136).
 *
 * A bot is created pending and does nothing until it is approved. It
 * trades a PAPER broker only, through the same Risk Engine as a manual
 * order, on a delayed quote. Everything shown is what the API returned:
 * equity and return stay blank until a cycle has actually valued the bot.
 */

type BrokerRow = { id: string; name: string; kind: string };

const STATUS_TONE: Record<LibraryBot["status"], "pos" | "neg" | "warn" | "neutral"> = {
  active: "pos",
  pending_approval: "warn",
  paused: "warn",
  stopped: "neutral",
};

function detailOf(data: unknown, status: number): string {
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: string };
    if (first?.msg) return first.msg;
  }
  return `Request failed (HTTP ${status})`;
}

function money(value: string | null): string {
  if (value === null) return "—";
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : value;
}

export function BotCard({
  bot,
  runs,
  strategyName,
  busy,
  onAction,
}: {
  bot: LibraryBot;
  runs: LibraryBotRun[];
  strategyName: string;
  busy: boolean;
  onAction: (action: string) => void;
}) {
  const ret = bot.return_pct === null ? null : Number(bot.return_pct);
  const held = Object.entries(bot.positions);
  const weights = bot.target_weights ? Object.entries(bot.target_weights) : null;
  const refused = Object.entries(bot.target_refused);
  return (
    <article className="flex flex-col gap-3 rounded-lg border border-line bg-surface p-4" data-testid={`library-bot-${bot.id}`}>
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold text-ink">{bot.name}</span>
          <Pill tone={STATUS_TONE[bot.status]}>{bot.status.replace("_", " ")}</Pill>
          <span className="font-mono text-[11px] text-ink-faint">{strategyName} · {bot.symbols.join(" ")}</span>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {bot.status === "pending_approval" ? <button type="button" disabled={busy} className={btnGhost} onClick={() => onAction("approve")}>Approve</button> : null}
          {bot.status === "active" ? <button type="button" disabled={busy} className={btnGhost} onClick={() => onAction("run")}>Run now</button> : null}
          {bot.status === "active" ? <button type="button" disabled={busy} className={btnGhost} onClick={() => onAction("pause")}>Pause</button> : null}
          {bot.status === "paused" ? <button type="button" disabled={busy} className={btnGhost} onClick={() => onAction("resume")}>Resume</button> : null}
          {bot.status !== "stopped" ? <button type="button" disabled={busy} className={btnGhost} onClick={() => onAction("stop")}>Stop</button> : null}
          {bot.status === "stopped" || bot.status === "pending_approval" ? <button type="button" disabled={busy} className={btnGhost} onClick={() => onAction("delete")}>Delete</button> : null}
        </div>
      </header>

      {bot.paused_reason ? <Alert tone="warn" role="status">{bot.paused_reason}</Alert> : null}

      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 rounded-md bg-well p-3 text-xs sm:grid-cols-5">
        <div><dt className="text-ink-faint">capital</dt><dd className="font-mono text-ink">{money(bot.capital)}</dd></div>
        <div><dt className="text-ink-faint">equity</dt><dd className="font-mono text-ink">{money(bot.last_equity)}</dd></div>
        <div>
          <dt className="text-ink-faint">return</dt>
          <dd className={`font-mono ${ret === null ? "text-ink-faint" : ret >= 0 ? "text-pos" : "text-neg"}`} data-testid="library-bot-return">
            {ret === null ? "—" : `${ret > 0 ? "+" : ""}${ret.toFixed(2)}%`}
          </dd>
        </div>
        <div><dt className="text-ink-faint">cash</dt><dd className="font-mono text-ink">{money(bot.cash)}</dd></div>
        <div><dt className="text-ink-faint">kill switch</dt><dd className="font-mono text-ink">{bot.max_drawdown_pct ? `-${Number(bot.max_drawdown_pct)}% from peak` : "off"}</dd></div>
      </dl>

      <div className="grid gap-3 text-xs lg:grid-cols-2">
        <div>
          <h4 className="mb-1 font-semibold text-ink-muted">Holding</h4>
          {held.length === 0 ? (
            <p className="text-ink-faint">Nothing - all cash.</p>
          ) : (
            <ul className="flex flex-col gap-1 font-mono text-ink">
              {held.map(([symbol, quantity]) => (
                <li key={symbol}>{Number(quantity)} {symbol}</li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <h4 className="mb-1 font-semibold text-ink-muted">
            The rule says{bot.last_decision_date ? ` (decided ${bot.last_decision_date})` : ""}
          </h4>
          {weights === null ? (
            <p className="text-ink-faint">Not evaluated yet - approve the bot and run it.</p>
          ) : weights.length === 0 ? (
            <p className="text-ink-faint">Hold cash.</p>
          ) : (
            <ul className="flex flex-col gap-1">
              {weights.map(([symbol, weight]) => (
                <li key={symbol}>
                  <span className="font-mono text-ink">{(weight * 100).toFixed(0)}% {symbol}</span>
                  {bot.target_explain[symbol] ? <span className="text-ink-muted"> - {bot.target_explain[symbol]}</span> : null}
                </li>
              ))}
            </ul>
          )}
          {refused.map(([symbol, reason]) => (
            <p key={symbol} className="mt-1 text-neg">{symbol} not bought: {reason}</p>
          ))}
        </div>
      </div>

      {runs.length > 0 ? (
        <details>
          <summary className="cursor-pointer text-xs font-semibold text-ink-muted">Recent cycles ({runs.length})</summary>
          <ul className="mt-2 flex flex-col gap-1.5 text-xs">
            {runs.map((run) => (
              <li key={run.id} className="rounded-md bg-well px-3 py-2">
                <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-[11px] text-ink-faint">
                  <span>{new Date(run.started_at).toLocaleString()}</span>
                  <span>{run.status.replaceAll("_", " ")}{run.equity ? ` · equity ${money(run.equity)}` : ""}</span>
                </div>
                {run.detail ? <p className="mt-0.5 text-ink-muted">{run.detail}</p> : null}
                {run.orders.map((order, k) => (
                  <p key={k} className="mt-0.5 font-mono text-ink">
                    {order.outcome} · {order.side} {Number(order.quantity)} {order.symbol} @ {Number(order.price)} ({order.quote_source})
                    {order.detail ? <span className="font-sans text-ink-muted"> - {order.detail}</span> : null}
                  </p>
                ))}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </article>
  );
}

export default function LibraryBots({
  strategies,
  strategy,
  symbols,
  params,
}: {
  strategies: LibraryStrategy[];
  strategy: LibraryStrategy | null;
  symbols: string[];
  params: Record<string, number>;
}) {
  const [bots, setBots] = useState<LibraryBot[]>([]);
  const [runs, setRuns] = useState<Record<string, LibraryBotRun[]>>({});
  const [brokers, setBrokers] = useState<BrokerRow[]>([]);
  const [brokerId, setBrokerId] = useState("");
  const [name, setName] = useState("");
  const [capital, setCapital] = useState("5000");
  const [killSwitch, setKillSwitch] = useState("20");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/library/bots?limit=50&offset=0", { cache: "no-store" });
      if (handleExpiredSession(res.status)) return;
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setError(detailOf(data, res.status));
        return;
      }
      const items = (data as { items: LibraryBot[] }).items;
      setBots(items);
      const pairs = await Promise.all(
        items.slice(0, 12).map(async (bot) => {
          const r = await fetch(`/api/library/bots/${bot.id}/runs?limit=5&offset=0`, { cache: "no-store" });
          const body = r.ok ? ((await r.json().catch(() => null)) as { items: LibraryBotRun[] } | null) : null;
          return [bot.id, body?.items ?? []] as const;
        }),
      );
      setRuns(Object.fromEntries(pairs));
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    }
  }, []);

  useEffect(() => {
    void Promise.resolve().then(load);
    void (async () => {
      try {
        const res = await fetch("/api/brokers?limit=50&offset=0", { cache: "no-store" });
        if (!res.ok) return;
        const data = (await res.json().catch(() => null)) as { brokers?: BrokerRow[] } | null;
        setBrokers((data?.brokers ?? []).filter((b) => b.kind === "paper"));
      } catch {
        /* the form says there is no paper broker */
      }
    })();
  }, [load]);

  const effectiveBroker = brokerId || brokers[0]?.id || "";

  async function create() {
    if (!strategy) return;
    setBusy("create");
    setError(null);
    setNotice(null);
    try {
      const res = await fetch("/api/library/bots", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          broker_id: effectiveBroker,
          strategy_id: strategy.id,
          symbols,
          params,
          capital,
          max_drawdown_pct: killSwitch.trim() === "" ? null : killSwitch,
        }),
      });
      if (handleExpiredSession(res.status)) return;
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setError(detailOf(data, res.status));
        return;
      }
      setNotice("Bot created. It does nothing until it is approved.");
      setName("");
      await load();
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setBusy(null);
    }
  }

  async function act(bot: LibraryBot, action: string) {
    setBusy(bot.id);
    setError(null);
    setNotice(null);
    try {
      const res = await fetch(`/api/library/bots/${bot.id}/${action}`, { method: "POST" });
      if (handleExpiredSession(res.status)) return;
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setError(detailOf(data, res.status));
        return;
      }
      if (action === "run") {
        const out = data as { run_status: string | null; run_detail: string | null };
        setNotice(`${bot.name}: ${out.run_status ?? "no run"}${out.run_detail ? ` - ${out.run_detail}` : ""}`);
      }
      await load();
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setBusy(null);
    }
  }

  const nameOf = (id: string) => strategies.find((s) => s.id === id)?.name ?? id;
  const canCreate = Boolean(strategy && effectiveBroker && symbols.length > 0 && Number(capital) > 0);

  return (
    <div className="flex flex-col gap-5">
      <Panel
        title="Run this rule as a paper bot"
        description="The bot holds what the rule holds, on a paper broker, through the same Risk Engine as a manual order. Decisions made on one close are traded during the next session on a delayed quote. It needs approval before it does anything."
      >
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            void create();
          }}
        >
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Bot name" hint="Optional.">
              <input id="lib-bot-name" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} placeholder={strategy?.name ?? ""} className={inputClass} />
            </Field>
            <Field label="Paper broker" hint={brokers.length === 0 ? "No paper broker is available to you." : "Paper brokers only."}>
              <select id="lib-bot-broker" value={effectiveBroker} onChange={(e) => setBrokerId(e.target.value)} className={inputClass}>
                {brokers.map((b) => (
                  <option key={b.id} value={b.id}>{b.name}</option>
                ))}
              </select>
            </Field>
            <Field label="Capital" hint="What the bot manages. Any one position is capped at 10% of the whole paper account.">
              <input id="lib-bot-capital" type="number" min={100} step="any" value={capital} onChange={(e) => setCapital(e.target.value)} className={monoInputClass} />
            </Field>
            <Field label="Kill switch %" hint="Sell and pause when the bot is this far below its peak. Blank = off.">
              <input id="lib-bot-kill" type="number" min={1} max={90} step="any" value={killSwitch} onChange={(e) => setKillSwitch(e.target.value)} className={monoInputClass} />
            </Field>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <button type="submit" disabled={!canCreate || busy === "create"} className={btnPrimary}>
              {busy === "create" ? "Creating…" : "Create paper bot"}
            </button>
            <span className="text-xs text-ink-faint">
              {strategy ? `${strategy.name} on ${symbols.length > 0 ? symbols.join(" ") : "no symbols yet"}` : "Choose a strategy above."}
            </span>
          </div>
        </form>
      </Panel>

      {error ? <Alert testId="library-bots-error">{error}</Alert> : null}
      {notice ? <Alert tone="info" role="status">{notice}</Alert> : null}

      <Panel title="Your library bots" description="Paper only. Equity is the bot's own cash plus its own positions at the last quotes it saw.">
        {bots.length === 0 ? (
          <EmptyNote>No library bots yet.</EmptyNote>
        ) : (
          <div className="flex flex-col gap-3">
            {bots.map((bot) => (
              <BotCard
                key={bot.id}
                bot={bot}
                runs={runs[bot.id] ?? []}
                strategyName={nameOf(bot.strategy_id)}
                busy={busy === bot.id}
                onAction={(action) => void act(bot, action)}
              />
            ))}
          </div>
        )}
      </Panel>
    </div>
  );
}
