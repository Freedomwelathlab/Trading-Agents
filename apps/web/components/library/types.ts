/** Strategy Library wire types (Phase 108, D135) - mirror the API exactly. */

export type Citation = {
  authors: string;
  year: number;
  title: string;
  venue: string;
  url: string | null;
};

export type ParamSpec = {
  key: string;
  label: string;
  default: number;
  minimum: number;
  maximum: number;
  help: string;
  integer: boolean;
};

export type LibraryStrategy = {
  id: string;
  name: string;
  family: string;
  cadence: string;
  summary: string;
  rules: string[];
  evidence: Citation[];
  published_year: number;
  params: ParamSpec[];
  caveats: string[];
  min_symbols: number;
};

export type Preset = {
  id: string;
  label: string;
  market: string;
  symbols: string[];
  benchmark: string[];
  strategies: string[];
  note: string;
  survivorship: boolean;
};

export type Catalogue = {
  strategies: LibraryStrategy[];
  presets: Preset[];
  cost_bps: Record<string, number>;
  data_source: string;
  max_symbols: number;
  disclaimer: string;
};

export type Metrics = {
  start: string;
  end: string;
  days: number;
  years: number;
  total_return: number;
  cagr: number | null;
  volatility: number | null;
  sharpe: number | null;
  sortino: number | null;
  max_drawdown: number;
  calmar: number | null;
  t_stat: number | null;
  best_year: number | null;
  worst_year: number | null;
  positive_years: number;
  total_years: number;
};

export type Segment = { label: string; strategy: Metrics | null; benchmark: Metrics | null };

export type Holding = { symbol: string; weight: number; note: string | null };

export type SymbolData = {
  symbol: string;
  vendor_symbol: string;
  currency: string | null;
  first: string;
  last: string;
  bars: number;
  last_close: number;
  cost_bps: number;
  removed: string[];
  truncated_before: string | null;
};

export type BacktestResult = {
  strategy_id: string;
  symbols: string[];
  benchmark_symbols: string[];
  params: Record<string, number>;
  lag: number;
  published_year: number;
  segments: Segment[];
  verdict: { code: string; headline: string; detail: string };
  curve: { d: string; s: number; b: number | null }[];
  years: { year: number; strategy: number; benchmark: number | null }[];
  trades: number;
  turnover_per_year: number | null;
  cost_drag_per_year: number | null;
  avg_exposure: number;
  target: Holding[];
  decided_on: string | null;
  data: SymbolData[];
  warnings: string[];
  source: string;
  as_of: string;
  generated_at: string;
};

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const v = value * 100;
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

export function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toFixed(digits);
}
