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

export type LibraryBot = {
  id: string;
  name: string;
  broker_id: string;
  status: "pending_approval" | "active" | "paused" | "stopped";
  strategy_id: string;
  symbols: string[];
  params: Record<string, number>;
  capital: string;
  max_drawdown_pct: string | null;
  cash: string;
  positions: Record<string, string>;
  target_weights: Record<string, number> | null;
  target_explain: Record<string, string>;
  target_refused: Record<string, string>;
  data_as_of: string | null;
  last_decision_date: string | null;
  last_equity: string | null;
  peak_equity: string | null;
  return_pct: string | null;
  approved_at: string | null;
  paused_reason: string | null;
  stopped_at: string | null;
  last_evaluated_at: string | null;
  created_at: string;
};

export type LibraryBotOrder = {
  symbol: string;
  side: string;
  quantity: string;
  price: string;
  quote_as_of: string;
  quote_source: string;
  outcome: string;
  detail: string | null;
  order_id: string | null;
};

export type LibraryBotRun = {
  id: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  orders_filled: number;
  orders_rejected: number;
  equity: string | null;
  decision_date: string | null;
  target: Record<string, number> | null;
  orders: LibraryBotOrder[];
  detail: string | null;
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
