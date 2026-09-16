"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { handleExpiredSession } from "@/lib/session";
import AppShell from "@/components/shell/AppShell";
import {
  Alert,
  EmptyNote,
  Panel,
  Pill,
  Stat,
} from "@/components/ui/primitives";

type HistoryEntry = {
  id: string;
  strategy_id: string;
  strategy_name: string;
  version_number: number;
  symbol: string;
  bar_interval: string;
  start_date: string;
  end_date: string;
  starting_cash: string;
  status: "succeeded" | "failed" | "running" | "pending";
  final_equity: string | null;
  total_return_pct: string | null;
  max_drawdown_pct: string | null;
  win_rate_pct: string | null;
  num_trades: number | null;
  fee_bps?: string | null;
  slippage_bps?: string | null;
  total_fees?: string | null;
  total_slippage?: string | null;
  error_detail: string | null;
  created_at: string;
};

function formatDetail(detail: unknown): string | null {
  if (detail == null) return null;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === "object") {
          const rec = item as Record<string, unknown>;
          const loc = Array.isArray(rec.loc) ? rec.loc.join(".") : undefined;
          const msg = typeof rec.msg === "string" ? rec.msg : JSON.stringify(item);
          return loc ? `${loc}: ${msg}` : msg;
        }
        return String(item);
      })
      .join("; ");
  }
  return JSON.stringify(detail);
}

function signTone(raw: string | null | undefined): "pos" | "neg" | undefined {
  const n = Number(raw);
  if (raw == null || raw === "" || !Number.isFinite(n) || n === 0) return undefined;
  return n > 0 ? "pos" : "neg";
}

function statusTone(s: string): "pos" | "neg" | "warn" | "neutral" {
  if (s === "succeeded") return "pos";
  if (s === "failed") return "neg";
  return "neutral";
}

/**
 * Number formatted to `dp` places, or an em dash when the backend sent
 * null. NEVER `0` for a null: a null metric means "not computed" — a
 * FAILED run computed none of them — and rendering that as 0.00% would
 * put a fabricated figure on screen, which is the one thing this
 * dashboard must not do.
 */
function num(raw: string | null | undefined, dp = 2): string {
  if (raw == null || raw === "") return "—";
  const n = Number(raw);
  return Number.isFinite(n) ? n.toFixed(dp) : "—";
}

/**
 * Cross-strategy backtest history (Phase 71, D089).
 *
 * Every backtest the user has ever run, newest first, in one place. The
 * per-version list under a strategy answers "what has this version done";
 * this answers "what have I tried, and how did it compare", which is the
 * question that matters once there is more than one idea — and it is the
 * surface that makes an earlier result findable months later rather than
 * lost behind a strategy id nobody remembers.
 *
 * FAILED runs are shown, not filtered out. A run that could not complete
 * records what was attempted and why it could not be answered, most often
 * an honest "not enough history for this indicator's warmup". Hiding them
 * would make this a history only of the attempts that happened to work.
 */
export default function BacktestHistoryPage() {
  const [items, setItems] = useState<HistoryEntry[] | null>(null);
  // `true` because this page fetches on mount and the fetch is deferred by
  // one microtask; `false` would render a frame of the empty state first.
  const [loading, setLoading] = useState(true);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [symbol, setSymbol] = useState("");

  async function load(symbolFilter: string) {
    setLoading(true);
    setErrorDetail(null);
    try {
      const q = new URLSearchParams({ limit: "200" });
      if (symbolFilter.trim()) q.set("symbol", symbolFilter.trim());
      const res = await fetch(`/api/backtest-runs?${q}`, { cache: "no-store" });
      const data = (await res.json().catch(() => null)) as
        | { items?: HistoryEntry[]; detail?: unknown }
        | null;
      if (!res.ok) {
        if (handleExpiredSession(res.status)) return;
        setItems(null);
        setErrorDetail(formatDetail(data?.detail) ?? `Request failed (HTTP ${res.status})`);
        return;
      }
      setItems(data?.items ?? []);
    } catch {
      setItems(null);
      setErrorDetail("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // Deferred by one microtask so the first `setState` lands after this
    // effect returns rather than during it — calling it synchronously
    // re-renders from inside the effect React is still committing.
    void Promise.resolve().then(() => load(""));
    // Loads once on mount; later loads are the explicit filter action.
  }, []);

  const succeeded = items?.filter((r) => r.status === "succeeded") ?? [];
  const best = succeeded.reduce<HistoryEntry | null>((acc, r) => {
    const v = Number(r.total_return_pct);
    if (!Number.isFinite(v)) return acc;
    return acc == null || v > Number(acc.total_return_pct) ? r : acc;
  }, null);
  const totalCosts = succeeded.reduce(
    (sum, r) => sum + Number(r.total_fees ?? 0) + Number(r.total_slippage ?? 0),
    0,
  );

  return (
    <AppShell
      title="Backtest history"
      subtitle="Every backtest you have run, newest first — across all strategies. Failed runs included: what was attempted and why it could not be answered is part of the record."
    >
      <div className="flex flex-col gap-5">
        {errorDetail && <Alert tone="error">{errorDetail}</Alert>}

        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          <Stat label="runs" value={items?.length ?? "—"} testId="history-count" />
          <Stat label="succeeded" value={items ? succeeded.length : "—"} tone="accent" />
          <Stat
            label="best return %"
            value={best ? num(best.total_return_pct) : "—"}
            tone={best ? signTone(best.total_return_pct) : undefined}
            hint={best ? `${best.strategy_name} · ${best.symbol}` : undefined}
          />
          <Stat
            label="costs paid, all runs"
            value={items ? totalCosts.toFixed(2) : "—"}
            hint="fees + slippage"
          />
        </div>

        <Panel
          title="All runs"
          actions={
            <form
              className="flex items-center gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                void load(symbol);
              }}
            >
              <input
                id="history-symbol-filter"
                aria-label="Filter by symbol"
                placeholder="symbol, e.g. BTC-USD"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                className="rounded border px-2 py-1 text-xs"
                style={{
                  background: "var(--well)",
                  borderColor: "var(--line)",
                  color: "var(--ink)",
                }}
              />
              <button
                type="submit"
                className="rounded border px-2 py-1 text-xs"
                style={{ borderColor: "var(--line)", color: "var(--ink-muted)" }}
              >
                Filter
              </button>
            </form>
          }
        >
          {loading && items === null && !errorDetail && <EmptyNote>Loading history…</EmptyNote>}

          {items !== null && items.length === 0 && !errorDetail && (
            <EmptyNote>
              No backtests yet{symbol.trim() ? ` for ${symbol.trim()}` : ""}. Run one from a
              strategy version — nothing here is generated or seeded.
            </EmptyNote>
          )}

          {items !== null && items.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs" style={{ borderCollapse: "collapse" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--line)" }}>
                    {[
                      "strategy",
                      "symbol",
                      "iv",
                      "window",
                      "return %",
                      "maxDD %",
                      "trades",
                      "win %",
                      "costs",
                      "status",
                      "",
                    ].map((h, i) => (
                      <th
                        key={h + i}
                        className="px-2 py-2 text-left font-mono uppercase"
                        style={{ color: "var(--ink-faint)", fontSize: "10px" }}
                      >
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {items.map((r) => (
                    <tr
                      key={r.id}
                      data-testid="history-row"
                      style={{ borderBottom: "1px solid var(--line)" }}
                    >
                      <td className="px-2 py-2" style={{ color: "var(--ink)" }}>
                        {r.strategy_name}
                        <span style={{ color: "var(--ink-faint)" }}> v{r.version_number}</span>
                      </td>
                      <td className="px-2 py-2 font-mono">{r.symbol}</td>
                      <td className="px-2 py-2 font-mono" style={{ color: "var(--ink-faint)" }}>
                        {r.bar_interval}
                      </td>
                      <td className="px-2 py-2 font-mono" style={{ color: "var(--ink-muted)" }}>
                        {r.start_date} → {r.end_date}
                      </td>
                      <td
                        className="px-2 py-2 text-right font-mono tabular-nums"
                        style={{
                          color:
                            signTone(r.total_return_pct) === "pos"
                              ? "var(--pos)"
                              : signTone(r.total_return_pct) === "neg"
                                ? "var(--neg)"
                                : "var(--ink-muted)",
                        }}
                      >
                        {num(r.total_return_pct)}
                      </td>
                      <td className="px-2 py-2 text-right font-mono tabular-nums">
                        {num(r.max_drawdown_pct)}
                      </td>
                      <td className="px-2 py-2 text-right font-mono tabular-nums">
                        {r.num_trades ?? "—"}
                      </td>
                      <td className="px-2 py-2 text-right font-mono tabular-nums">
                        {num(r.win_rate_pct, 1)}
                      </td>
                      <td
                        className="px-2 py-2 text-right font-mono tabular-nums"
                        style={{ color: "var(--ink-muted)" }}
                        title={
                          r.fee_bps == null
                            ? "This run predates cost modelling — its returns are frictionless, which is not the same as zero cost."
                            : `${r.fee_bps}bps fee + ${r.slippage_bps}bps slippage`
                        }
                      >
                        {r.total_fees == null && r.total_slippage == null
                          ? "—"
                          : (Number(r.total_fees ?? 0) + Number(r.total_slippage ?? 0)).toFixed(2)}
                      </td>
                      <td className="px-2 py-2">
                        <Pill tone={statusTone(r.status)}>{r.status}</Pill>
                      </td>
                      <td className="px-2 py-2">
                        <Link
                          href={`/strategies/${r.strategy_id}/backtests/${r.id}`}
                          style={{ color: "var(--accent)" }}
                        >
                          open
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
    </AppShell>
  );
}
