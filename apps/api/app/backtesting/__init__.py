"""Backtesting engine (D025): replays a single hard-coded SMA(20)-crossover
strategy against real historical daily closes (marketdata.HistoryProvider,
D021) through the real deterministic Risk Engine (risk.engine.evaluate_trade,
D004) and the real PaperBrokerAdapter fill math (execution.paper_broker,
D014) - a fresh in-memory instance per run, never apps/api/app/execution's
load_paper_broker/save_paper_broker, so this module structurally cannot
touch a real broker's persisted state. See docs/DECISIONS.md D025.
"""
