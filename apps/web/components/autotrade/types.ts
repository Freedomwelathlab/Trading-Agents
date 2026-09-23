/** Shapes returned by `/autotrade/*` (Phase 81, D098). Every numeric field
 * is a JSON string (`Decimal`) and is rendered as received. */

export type Bot = {
  id: string;
  name: string;
  broker_id: string;
  status: "pending_approval" | "active" | "paused" | "stopped";
  watchlist_id: string | null;
  symbols: string[];
  market_type: string;
  bar_interval: string;
  max_trades_per_session: number;
  max_trades_per_day: number;
  capital_per_trade: string;
  strategy_mode: string;
  setups: string[];
  min_score: number;
  extended_hours_min_score: number | null;
  allow_short: boolean;
  stop_loss_mode: string;
  stop_loss_max_pct: string | null;
  trailing_stop_pct: string | null;
  take_profit_mode: string;
  take_profit_min_pct: string | null;
  trailing_take_profit_pct: string | null;
  news_blackout_minutes: number;
  approved_at: string | null;
  paused_reason: string | null;
  stopped_at: string | null;
  last_evaluated_at: string | null;
  created_at: string;
};

export type BotRun = {
  id: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  symbols_scanned: number;
  signals_found: number;
  signals_skipped: number;
  trades_opened: number;
  trades_closed: number;
  setups_active: string[];
  detail: string | null;
};

export type BotTrade = {
  id: string;
  symbol: string;
  session_date: string;
  setup_name: string;
  score: number;
  evidence: Record<string, string>;
  quantity: string;
  entry_price: string;
  initial_stop_price: string;
  stop_price: string;
  take_profit_price: string | null;
  peak_price: string;
  trough_price: string;
  take_profit_armed: boolean;
  opened_at: string;
  closed_at: string | null;
  exit_price: string | null;
  exit_reason: string | null;
  realized_pnl: string | null;
  r_multiple: string | null;
};

export type SetupStat = {
  setup_name: string;
  trades: number;
  wins: number;
  win_rate: string | null;
  expectancy_r: string | null;
  total_r: string;
  total_pnl: string;
  demoted: boolean;
  symbol: string | null;
  hour: number | null;
  avg_mfe_r: string | null;
  avg_mae_r: string | null;
};

export type BotStats = {
  bot_id: string;
  strategy_mode: string;
  configured_setups: string[];
  active_setups: string[];
  setups: SetupStat[];
  by_symbol: SetupStat[];
  by_hour: SetupStat[];
  closed_trades: number;
  total_r: string;
  total_pnl: string;
};

export type BotInsight = {
  id: string;
  session_date: string;
  trades: number;
  wins: number;
  total_r: string;
  total_pnl: string;
  best_setup: string | null;
  worst_setup: string | null;
  findings: string[];
  demoted_setups: string[];
  created_at: string;
};

export type RunNow = {
  status: string;
  detail: string | null;
  run_status: string | null;
  run_detail: string | null;
  symbols_scanned: number;
  signals_found: number;
  trades_opened: number;
  trades_closed: number;
};
