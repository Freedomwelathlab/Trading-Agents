"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { handleExpiredSession } from "@/lib/session";
import {
  Alert,
  Field,
  Panel,
  Pill,
  btnPrimary,
  inputClass,
  monoInputClass,
} from "@/components/ui/primitives";
import BacktestReport from "./BacktestReport";
import type { BacktestResult, Catalogue, LibraryStrategy, Preset } from "./types";

/**
 * The Strategy Library (Phase 108, D135): published daily-bar strategies,
 * each with its rules, its source and its known weaknesses, runnable on
 * real adjusted history for a US, Singapore, India, crypto or FX universe.
 *
 * Nothing is run until the button is pressed, and nothing is shown that
 * the API did not return: a failed run shows the API's own refusal.
 */

const CUSTOM = "custom";

function detailOf(data: unknown, status: number): string {
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: string };
    if (first?.msg) return first.msg;
  }
  return `Request failed (HTTP ${status})`;
}

export default function LibraryWorkbench() {
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [strategyId, setStrategyId] = useState<string>("");
  const [presetId, setPresetId] = useState<string>("");
  const [symbols, setSymbols] = useState("");
  const [benchmark, setBenchmark] = useState("");
  const [params, setParams] = useState<Record<string, string>>({});
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestResult | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await fetch("/api/library/strategies", { cache: "no-store" });
        if (handleExpiredSession(res.status)) return;
        const data = await res.json().catch(() => null);
        if (!alive) return;
        if (!res.ok) {
          setLoadError(detailOf(data, res.status));
          return;
        }
        const cat = data as Catalogue;
        setCatalogue(cat);
        const first = cat.strategies[0];
        if (first) {
          setStrategyId(first.id);
          const preset = cat.presets.find((p) => p.strategies.includes(first.id));
          setPresetId(preset?.id ?? CUSTOM);
        }
      } catch {
        if (alive) setLoadError("DATA_UNAVAILABLE: could not reach the trading API");
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const strategy: LibraryStrategy | null = useMemo(
    () => catalogue?.strategies.find((s) => s.id === strategyId) ?? null,
    [catalogue, strategyId],
  );
  const presets: Preset[] = useMemo(
    () => catalogue?.presets.filter((p) => p.strategies.includes(strategyId)) ?? [],
    [catalogue, strategyId],
  );
  const preset = presets.find((p) => p.id === presetId) ?? null;

  function chooseStrategy(id: string) {
    setStrategyId(id);
    setParams({});
    setResult(null);
    setRunError(null);
    const next = catalogue?.presets.find((p) => p.strategies.includes(id));
    setPresetId(next?.id ?? CUSTOM);
  }

  const run = useCallback(async () => {
    if (!strategy) return;
    setRunning(true);
    setRunError(null);
    const body: Record<string, unknown> = { strategy_id: strategy.id };
    if (presetId !== CUSTOM) {
      body.preset_id = presetId;
    } else {
      body.symbols = symbols.split(/[\s,]+/).filter(Boolean);
      const bench = benchmark.split(/[\s,]+/).filter(Boolean);
      if (bench.length > 0) body.benchmark = bench;
    }
    const numeric: Record<string, number> = {};
    for (const [key, raw] of Object.entries(params)) {
      if (raw.trim() !== "" && Number.isFinite(Number(raw))) numeric[key] = Number(raw);
    }
    if (Object.keys(numeric).length > 0) body.params = numeric;
    try {
      const res = await fetch("/api/library/backtest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (handleExpiredSession(res.status)) return;
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setResult(null);
        setRunError(detailOf(data, res.status));
        return;
      }
      setResult(data as BacktestResult);
    } catch {
      setRunError("DATA_UNAVAILABLE: could not reach the trading API");
    } finally {
      setRunning(false);
    }
  }, [strategy, presetId, symbols, benchmark, params]);

  if (loadError) return <Alert>{loadError}</Alert>;
  if (!catalogue) return <p className="text-sm text-ink-faint">Loading the library…</p>;

  const families = [...new Set(catalogue.strategies.map((s) => s.family))];

  return (
    <div className="flex flex-col gap-5">
      <Alert tone="info" role="status">{catalogue.disclaimer}</Alert>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
        <nav aria-label="Strategies" className="flex flex-col gap-4">
          {families.map((family) => (
            <div key={family} className="flex flex-col gap-1.5">
              <span className="text-[11px] font-semibold uppercase tracking-[0.09em] text-ink-faint">{family}</span>
              {catalogue.strategies
                .filter((s) => s.family === family)
                .map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    aria-pressed={s.id === strategyId}
                    onClick={() => chooseStrategy(s.id)}
                    className={`flex cursor-pointer flex-col items-start gap-0.5 rounded-lg border px-3 py-2 text-left transition-colors ${
                      s.id === strategyId ? "border-accent bg-raised" : "border-line bg-surface hover:border-line-strong"
                    }`}
                  >
                    <span className="text-sm font-semibold text-ink">{s.name}</span>
                    <span className="font-mono text-[11px] text-ink-faint">{s.cadence} · published {s.published_year}</span>
                  </button>
                ))}
            </div>
          ))}
        </nav>

        {strategy ? (
          <div className="flex min-w-0 flex-col gap-5">
            <Panel title={strategy.name} description={strategy.summary}>
              <div className="flex flex-col gap-4">
                <div>
                  <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-[0.09em] text-ink-muted">The rule</h3>
                  <ol className="flex list-decimal flex-col gap-1 pl-5 text-sm text-ink">
                    {strategy.rules.map((r) => (
                      <li key={r}>{r}</li>
                    ))}
                  </ol>
                </div>
                <div>
                  <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-[0.09em] text-ink-muted">Where it comes from</h3>
                  <ul className="flex flex-col gap-1 text-sm text-ink-muted">
                    {strategy.evidence.map((c) => (
                      <li key={c.title}>
                        {c.authors} ({c.year}).{" "}
                        {c.url ? (
                          <a href={c.url} target="_blank" rel="noreferrer" className="text-accent underline-offset-2 hover:underline">{c.title}</a>
                        ) : (
                          <span className="text-ink">{c.title}</span>
                        )}
                        . <span className="text-ink-faint">{c.venue}</span>
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-[0.09em] text-ink-muted">Known weaknesses</h3>
                  <ul className="flex list-disc flex-col gap-1 pl-5 text-sm text-ink-muted">
                    {strategy.caveats.map((c) => (
                      <li key={c}>{c}</li>
                    ))}
                  </ul>
                </div>
              </div>
            </Panel>

            <Panel
              title="Measure it"
              description={`Real adjusted daily history from ${catalogue.data_source}, net of costs, against buy-and-hold. Results after ${strategy.published_year} - when the rule became public - are reported separately.`}
            >
              <form
                className="flex flex-col gap-4"
                onSubmit={(e) => {
                  e.preventDefault();
                  void run();
                }}
              >
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Universe" hint={preset?.note || undefined}>
                    <select
                      id="lib-universe"
                      value={presetId}
                      onChange={(e) => setPresetId(e.target.value)}
                      className={inputClass}
                    >
                      {presets.map((p) => (
                        <option key={p.id} value={p.id}>{p.market} · {p.label}</option>
                      ))}
                      <option value={CUSTOM}>Your own symbols</option>
                    </select>
                  </Field>
                  {strategy.params.map((p) => (
                    <Field key={p.key} label={p.label} hint={p.help}>
                      <input
                        id={`lib-param-${p.key}`}
                        type="number"
                        min={p.minimum}
                        max={p.maximum}
                        step={p.integer ? 1 : "any"}
                        value={params[p.key] ?? String(p.default)}
                        onChange={(e) => setParams((prev) => ({ ...prev, [p.key]: e.target.value }))}
                        className={monoInputClass}
                      />
                    </Field>
                  ))}
                </div>
                {presetId === CUSTOM ? (
                  <div className="grid gap-4 sm:grid-cols-2">
                    <Field
                      label="Symbols"
                      hint={`Up to ${catalogue.max_symbols}, separated by spaces or commas. SPY.US, D05.SG, RELIANCE.IN, BTC-USD, EURUSD.FX.`}
                    >
                      <input id="lib-symbols" value={symbols} onChange={(e) => setSymbols(e.target.value)} placeholder="SPY.US QQQ.US" className={monoInputClass} />
                    </Field>
                    <Field label="Buy-and-hold benchmark" hint="Optional. Held equally, rebalanced monthly.">
                      <input id="lib-benchmark" value={benchmark} onChange={(e) => setBenchmark(e.target.value)} placeholder="SPY.US" className={monoInputClass} />
                    </Field>
                  </div>
                ) : preset ? (
                  <div className="flex flex-wrap items-center gap-1.5">
                    {preset.symbols.map((s) => (
                      <Pill key={s}>{s}</Pill>
                    ))}
                    {preset.survivorship ? <Pill tone="warn">today&apos;s survivors</Pill> : null}
                  </div>
                ) : null}
                <div className="flex flex-wrap items-center gap-3">
                  <button type="submit" disabled={running} className={btnPrimary}>
                    {running ? "Measuring…" : "Run backtest"}
                  </button>
                  <span className="text-xs text-ink-faint">The first run of a universe fetches its history and can take up to a minute.</span>
                </div>
              </form>
            </Panel>

            {runError ? <Alert testId="library-error">{runError}</Alert> : null}
            {result ? (
              <Panel title="Result" description={`${result.symbols.join(", ")} · generated ${new Date(result.generated_at).toLocaleString()}`}>
                <BacktestReport result={result} />
              </Panel>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
