# Trading Safety — NON-NEGOTIABLE

Governing spec: §3, §17, §38, §46, §51, §56, §57, §62. This document does not
override the spec; it's the fast-reference version for day-to-day work.

## The pipeline (target)

```
LLM (proposal only)
  ↓
Trade Proposal
  ↓
Deterministic Risk Engine  ← non-LLM, must run even if every LLM is down
  ↓
Portfolio Decision
  ↓
Execution Policy
  ↓
OMS
  ↓
Broker Adapter
```

An LLM never sits below the Risk Engine in this chain. No shortcut, no
"trusted agent" exception.

The "Portfolio Decision" step exists as of Phase 26 (D029):
`apps/api/app/portfolio_manager/`, deterministic and LLM-free like the
Risk Engine above it. It can only ever shrink or stop a trade the Risk
Engine already approved, and when it shrinks one, the resized proposal
goes back through the Risk Engine before any broker call — so nothing
below the Risk Engine in this chain, not even this system's own sizing
logic, can hand a broker a quantity the Risk Engine has not approved.

## The LLM must never bypass

Position limits · risk limits · exposure limits · buying power · stop
requirements · market-data freshness checks · duplicate-order checks ·
current trading mode · emergency stop.

## The LLM is never the source of truth for

Prices, balances, position quantities, P&L, risk calculations, order status,
exposure, margin, or position sizing (spec §62). These are always computed
or fetched deterministically and handed to the LLM as read-only context.

## Fail closed

If safety cannot be verified — stale data, unreachable risk service, unknown
account state — **block the trade**. Never fall back to "probably fine."

## Requires explicit user confirmation before Claude proceeds

Enabling live trading · using real broker credentials · placing a real order
· deleting production data · destructive migrations · disabling any safety
control. "The user asked for a feature" is not itself confirmation to flip
`LIVE_TRADING_ENABLED` — ask again, specifically, at that moment.

## No fabrication

Never invent market prices, fills, P&L, signals, reports, or broker
responses. Missing or unreachable data renders as `NOT CONFIGURED` or
`DATA_UNAVAILABLE:` — a sentinel, not a guess.

## Currently enforced in code

- `apps/api/app/core/config.py` — `Settings` raises at construction if
  `trading_mode=live` and `live_trading_enabled` is not `true`.
- `apps/api/app/core/logging.py` — structlog processor redacts any log field
  whose key matches a secret/password/token/key pattern.
- `apps/api/app/safety/emergency_stop.py` + `emergency_stop_events` —
  spec §46's emergency stop (D039). The authoritative state is a persisted,
  append-only, audited row, flipped live by an `admin:manage` holder over
  `POST /admin/emergency-stop` (a reason is required) with no restart or
  `.env` edit. The trade route reads it on every submission and hands the
  boolean to the Risk Engine, which stays zero-I/O and rejects every trade
  with `EMERGENCY_STOP_ACTIVE` while it is on. `EMERGENCY_STOP_ACTIVE` in
  `.env` is now only the bootstrap default used before the first-ever flip;
  it does not override a persisted row.

## Not yet enforced (because not yet built)

Broker adapter beyond the paper simulator, and the full audit/decision-chain
tables. Do not write code that assumes any of these exist.
