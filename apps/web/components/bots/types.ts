/** Wire shapes of the bot scan dashboards (Phase 107, D133/D134). */

export type SetupScan = {
  setup: string;
  enabled: boolean;
  fired: boolean;
  direction: "long" | "short" | null;
  score: number | null;
  entry: string | null;
  stop: string | null;
  target_1r: string | null;
  risk_pct: string | null;
  confidence_pct: string | null;
  sample: number | null;
  verdict:
    | "qualifies"
    | "below_min_score"
    | "direction_not_allowed"
    | "stop_quality"
    | "not_enabled"
    | "no_signal";
};

export type SymbolBoard = {
  symbol: string;
  asset_class: string;
  last_close: string | null;
  last_ts: string | null;
  phase: string | null;
  recommendation: "BUY" | "SELL" | "WAIT";
  headline: string;
  best: SetupScan | null;
  setups: SetupScan[];
  bars_used: number;
  calibration: string;
};

export type BotScan = {
  bot_id: string;
  name: string;
  asset_class: string;
  market_type: string;
  bar_interval: string;
  min_score: number;
  directions: string[];
  generated_at: string;
  boards: SymbolBoard[];
  evidence: string[];
};

export type ProposalLeg = {
  contract_symbol: string;
  right: string;
  strike: string;
  side: string;
  bid: string;
  ask: string;
  fill_price: string;
  delta: string | null;
};

export type OptionsProposal = {
  structure_type: string;
  expiry: string;
  legs: ProposalLeg[];
  net_price: string;
  max_loss_per_contract: string;
  max_profit_per_contract: string | null;
  quantity: number;
  capital_at_risk: string;
  quote_source: string;
};

export type OptionsBotScan = {
  bot_id: string;
  name: string;
  underlying: string;
  mode: "signal" | "fixed";
  min_signal_score: number | null;
  generated_at: string;
  board: SymbolBoard;
  structure_type: string | null;
  proposal: OptionsProposal | null;
  proposal_note: string;
  evidence: string[];
};
