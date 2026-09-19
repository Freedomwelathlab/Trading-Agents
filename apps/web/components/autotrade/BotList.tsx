"use client";

import { useCallback, useEffect, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  EmptyNote,
  Panel,
  Pill,
  TableScroll,
  btnGhost,
  btnPrimary,
  btnSecondary,
  tableClass,
  tbodyRowClass,
  tdClass,
  thClass,
  theadRowClass,
} from "@/components/ui/primitives";
import type { Bot, BotRun, BotStats, BotTrade, RunNow } from "./types";

/**
 * Every bot the caller owns, its lifecycle actions, and — for the selected
 * one — its runs, its trades and the learning loop's per-setup numbers.
 * Everything shown is a row the backend returned; a run that did nothing
 * shows its own stated reason in the detail column.
 */

const STATUS_TONE: Record<Bot["status"], "neutral" | "pos" | "warn" | "neg"> = {
  pending_approval: "warn",
  active: "pos",
  paused: "neutral",
  stopped: "neg",
};

export default function BotList({ refreshKey }: { refreshKey: number }) {
  const [bots, setBots] = useState<Bot[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [runs, setRuns] = useState<BotRun[] | null>(null);
  const [trades, setTrades] = useState<BotTrade[] | null>(null);
  const [stats, setStats] = useState<BotStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadBots = useCallback(async () => {
    setError(null);
    try {
      const res = await fetch("/api/autotrade/bots", { cache: "no-store" });
      const data = (await res.json().catch(() => null)) as { bots?: Bot[]; detail?: string } | null;
      if (handleExpiredSession(res.status)) return;
      if (!res.ok) {
        setError(data?.detail ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setBots(data?.bots ?? []);
    } catch {
      setError("DATA_UNAVAILABLE: could not reach the trading API");
    }
  }, []);

  const loadDetail = useCallback(async (id: string) => {
    const get = async <T,>(path: string): Promise<T | null> => {
      const res = await fetch(`/api/autotrade/bots/${id}/${path}`, { cache: "no-store" });
      if (handleExpiredSession(res.status)) return null;
      if (!res.ok) return null;
      return (await res.json()) as T;
    };
    const [r, t, s] = await Promise.all([
      get<{ runs: BotRun[] }>("runs?limit=25"),
      get<{ trades: BotTrade[] }>("trades?limit=50"),
      get<BotStats>("stats"),
    ]);
    setRuns(r?.runs ?? []);
    setTrades(t?.trades ?? []);
    setStats(s);
  }, []);

  useEffect(() => {
    void Promise.resolve().then(loadBots);
  }, [loadBots, refreshKey]);

  useEffect(() => {
    if (!selected) return;
    void Promise.resolve().then(() => loadDetail(selected));
  }, [selected, loadDetail]);

  const current = bots?.find((b) => b.id === selected) ?? null;

  async function act(id: string, action: "approve" | "pause" | "resume" | "stop" | "run") {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await fetch(`/api/autotrade/bots/${id}/${action}`, { method: "POST" });
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      if (handleExpiredSession(res.status)) return;
      if (!res.ok) {
        setError((data?.detail as string | undefined) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      if (action === "run") {
        const r = data as unknown as RunNow;
        setNotice(
          `Cycle: ${r.status}${r.run_status ? ` · run ${r.run_status}` : ""} · scanned ${r.symbols_scanned}, ` +
            `signals ${r.signals_found}, opened ${r.trades_opened}, closed ${r.trades_closed}` +
            (r.run_detail ? ` — ${r.run_detail}` : r.detail ? ` — ${r.detail}` : ""),
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

  return (
    <div className="flex flex-col gap-5">
      <Panel
        title="Your bots"
        description="A bot trades only after you approve it. Pause steps it out of the rotation; stop is final (open positions are not auto-sold — close them on the desk)."
      >
        {error ? <Alert>{error}</Alert> : null}
        {notice ? <Alert tone="info" role="status">{notice}</Alert> : null}
        {bots === null ? (
          <p className="text-sm text-ink-faint">Loading…</p>
        ) : bots.length === 0 ? (
          <EmptyNote>No bots yet. Create one above.</EmptyNote>
        ) : (
          <TableScroll>
            <table className={tableClass}>
              <thead>
                <tr className={theadRowClass}>
                  <th className={thClass}>name</th>
                  <th className={thClass}>status</th>
                  <th className={thClass}>symbols</th>
                  <th className={thClass}>market</th>
                  <th className={thClass}>live / day</th>
                  <th className={thClass}>capital</th>
                  <th className={thClass}>strategy</th>
                  <th className={thClass}>last cycle</th>
                  <th className={thClass}>actions</th>
                </tr>
              </thead>
              <tbody>
                {bots.map((b) => (
                  <tr
                    key={b.id}
                    className={`${tbodyRowClass} ${b.id === selected ? "bg-well" : ""}`}
                  >
                    <td className={tdClass}>
                      <button type="button" className="underline-offset-2 hover:underline" onClick={() => setSelected(b.id)}>
                        {b.name}
                      </button>
                    </td>
                    <td className={tdClass}><Pill tone={STATUS_TONE[b.status]}>{b.status}</Pill></td>
                    <td className={tdClass}>{b.symbols.join(", ")}</td>
                    <td className={tdClass}>{b.market_type}</td>
                    <td className={tdClass}>{b.max_trades_per_session} / {b.max_trades_per_day}</td>
                    <td className={tdClass}>{b.capital_per_trade}</td>
                    <td className={tdClass}>{b.strategy_mode}{b.strategy_mode !== "auto" ? `: ${b.setups.join(",")}` : ""}</td>
                    <td className={tdClass}>{b.last_evaluated_at ? new Date(b.last_evaluated_at).toLocaleTimeString() : "—"}</td>
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
                      <button type="button" className={btnSecondary} disabled={busy} onClick={() => act(b.id, "run")} title="Run one cycle now">Scan now</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        )}
      </Panel>

      {current ? (
        <>
          <Panel
            title={`${current.name} — learning loop`}
            description="Per-setup results on this bot's own closed trades. In auto mode a setup with 20+ trades and expectancy below −0.10R is demoted (not run) until its numbers recover."
          >
            {stats === null ? (
              <p className="text-sm text-ink-faint">Loading…</p>
            ) : (
              <div className="flex flex-col gap-2">
                <p className="text-xs text-ink-muted">
                  Active setups: <span className="font-mono">{stats.active_setups.join(", ") || "none"}</span>
                  {" · "}closed trades {stats.closed_trades} · total R <span className="font-mono">{stats.total_r}</span> · P&amp;L <span className="font-mono">{stats.total_pnl}</span>
                </p>
                {stats.setups.length === 0 ? (
                  <EmptyNote>No closed trades yet — nothing to learn from.</EmptyNote>
                ) : (
                  <TableScroll>
                    <table className={tableClass}>
                      <thead>
                        <tr className={theadRowClass}>
                          <th className={thClass}>setup</th>
                          <th className={thClass}>trades</th>
                          <th className={thClass}>win %</th>
                          <th className={thClass}>expectancy R</th>
                          <th className={thClass}>total R</th>
                          <th className={thClass}>P&amp;L</th>
                          <th className={thClass}>state</th>
                        </tr>
                      </thead>
                      <tbody>
                        {stats.setups.map((s) => (
                          <tr key={s.setup_name} className={tbodyRowClass}>
                            <td className={tdClass}>{s.setup_name}</td>
                            <td className={tdClass}>{s.trades}</td>
                            <td className={tdClass}>{s.win_rate !== null ? (Number(s.win_rate) * 100).toFixed(0) : "—"}</td>
                            <td className={tdClass}>{s.expectancy_r ?? "—"}</td>
                            <td className={tdClass}>{s.total_r}</td>
                            <td className={tdClass}>{s.total_pnl}</td>
                            <td className={tdClass}><Pill tone={s.demoted ? "neg" : "pos"}>{s.demoted ? "demoted" : "active"}</Pill></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </TableScroll>
                )}
              </div>
            )}
          </Panel>

          <Panel title={`${current.name} — trades`} description="Every position the bot opened, with its bracket and, once closed, the exit and its R.">
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
                      <th className={thClass}>symbol</th>
                      <th className={thClass}>setup</th>
                      <th className={thClass}>score</th>
                      <th className={thClass}>qty</th>
                      <th className={thClass}>entry</th>
                      <th className={thClass}>stop</th>
                      <th className={thClass}>target</th>
                      <th className={thClass}>peak</th>
                      <th className={thClass}>exit</th>
                      <th className={thClass}>reason</th>
                      <th className={thClass}>P&amp;L</th>
                      <th className={thClass}>R</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trades.map((t) => (
                      <tr key={t.id} className={tbodyRowClass}>
                        <td className={tdClass}>{new Date(t.opened_at).toLocaleString()}</td>
                        <td className={tdClass}>{t.symbol}</td>
                        <td className={tdClass}>{t.setup_name}</td>
                        <td className={tdClass}>{t.score}</td>
                        <td className={tdClass}>{t.quantity}</td>
                        <td className={tdClass}>{t.entry_price}</td>
                        <td className={tdClass}>{t.stop_price}{t.stop_price !== t.initial_stop_price ? " ↑" : ""}</td>
                        <td className={tdClass}>{t.take_profit_price ?? "—"}{t.take_profit_armed ? " (armed)" : ""}</td>
                        <td className={tdClass}>{t.peak_price}</td>
                        <td className={tdClass}>{t.exit_price ?? <Pill tone="pos">open</Pill>}</td>
                        <td className={tdClass}>{t.exit_reason ?? "—"}</td>
                        <td className={tdClass}>{t.realized_pnl ?? "—"}</td>
                        <td className={tdClass}>{t.r_multiple ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableScroll>
            )}
          </Panel>

          <Panel title={`${current.name} — cycles`} description="One row per cycle. A cycle that placed nothing says why.">
            {runs === null ? (
              <p className="text-sm text-ink-faint">Loading…</p>
            ) : runs.length === 0 ? (
              <EmptyNote>No cycles yet — approve the bot, or press Scan now.</EmptyNote>
            ) : (
              <TableScroll>
                <table className={tableClass}>
                  <thead>
                    <tr className={theadRowClass}>
                      <th className={thClass}>started</th>
                      <th className={thClass}>status</th>
                      <th className={thClass}>scanned</th>
                      <th className={thClass}>signals</th>
                      <th className={thClass}>skipped</th>
                      <th className={thClass}>opened</th>
                      <th className={thClass}>closed</th>
                      <th className={thClass}>setups active</th>
                      <th className={thClass}>detail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((r) => (
                      <tr key={r.id} className={tbodyRowClass}>
                        <td className={tdClass}>{new Date(r.started_at).toLocaleString()}</td>
                        <td className={tdClass}><Pill tone={r.status === "succeeded" ? "pos" : r.status === "failed" ? "neg" : "neutral"}>{r.status}</Pill></td>
                        <td className={tdClass}>{r.symbols_scanned}</td>
                        <td className={tdClass}>{r.signals_found}</td>
                        <td className={tdClass}>{r.signals_skipped}</td>
                        <td className={tdClass}>{r.trades_opened}</td>
                        <td className={tdClass}>{r.trades_closed}</td>
                        <td className={tdClass}>{r.setups_active.join(", ")}</td>
                        <td className="max-w-[480px] whitespace-normal px-2.5 py-1.5 text-xs text-ink-muted">{r.detail ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableScroll>
            )}
          </Panel>
        </>
      ) : null}
    </div>
  );
}
