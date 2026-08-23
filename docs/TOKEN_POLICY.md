# Token Policy

No agent/LLM code exists yet — this is the policy to build against, not a
description of a running budget system.

## Default principle

Use the minimum context and output necessary to complete the task. Never cut
information required for correctness or trading safety to save tokens.

## Must stay deterministic (never delegate to an LLM)

RSI, MACD, EMA, SMA, ATR, VWAP, volatility, correlation, Sharpe, Sortino,
drawdown, position sizing, P&L, exposure, order validation, account balance,
broker state. These are numeric and auditable; an LLM must never compute or
approximate them.

## Budgets (to be set when the agent layer is designed)

Per-agent token budget, chat-completion budget, research-round budget, retry
limit, context-size limit, output-size limit, and fallback behavior are all
open decisions — see [DECISIONS.md](DECISIONS.md) for status. Do not invent
numbers here; set them alongside the first agent implementation and record
the decision.
