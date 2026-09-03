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

- `apps/api/app/execution/live_broker.py` — the live broker adapter
  (Phase 43, D058). **The live path exists in code and is inert by
  default.** `build_live_broker_adapter()` returns `None` — never a
  degraded or simulated stand-in — unless `TRADING_MODE=live` AND
  `LIVE_TRADING_ENABLED=true` AND all three `LONGPORT_LIVE_*` credentials
  are set together, so on the committed configuration no trade context is
  constructed and no connection is opened. The live trading credentials
  are separate settings from the read-only `LONGPORT_*` quote credentials:
  a quote key must never become a trading key.
- `apps/api/app/api/routes/trades.py` — the explicit per-trade live
  confirmation this document requires is now enforceable server-side
  (D058). A trade against a `kind=live` broker needs `"confirm": true` in
  that request's body (400 `LIVE_CONFIRMATION_REQUIRED` otherwise), a
  configured live path, and the `trade:submit:live` permission IN ADDITION
  to `trade:submit:paper`. A confirmation living only in a frontend dialog
  is not a confirmation the server can enforce, which is why this is a
  payload field and not a UI concern. `POST /brokers/{id}/agent-trades`
  remains paper-only: an agent-invented side/quantity is not something a
  human confirmed.
- Live trades are gated by the SAME emergency stop, duplicate-order check,
  deterministic Risk Engine, and Portfolio Manager as paper trades — the
  same code, not a parallel implementation — against the tighter
  `LIVE_RISK_*` limits (5% max position, 20% max exposure, 1% risk per
  trade). The live adapter never fabricates a fill: a broker order that is
  accepted but not executed surfaces as `502 LIVE_ORDER_UNCONFIRMED`
  naming the real order id, never as an assumed fill at the estimated
  price.
- A broker row's `kind` is the per-broker paper/live switch, managed by
  `POST /admin/brokers` and `PATCH /admin/brokers/{id}/mode` (D058).
  Designating a broker live requires `confirm_live: true`, and a broker
  that has already traded (or holds a simulated book) can no longer be
  flipped — that would make simulated and real history indistinguishable
  in an append-only audit trail.

- `apps/api/app/oms/persistence.py` + `apps/api/app/execution/reconciliation.py`
  — reconciliation of an accepted-but-unexecuted live order (Phase 49,
  D066). Two things changed, and the first matters even with the
  reconciler switched off:
  1. **A live order the broker accepts but does not execute is now
     RECORDED.** Before this phase it was not: the `502
     LIVE_ORDER_UNCONFIRMED` path rolled the request's transaction back, so
     the broker's order id survived only in a log line and the system's own
     append-only trail had no row for a real order at a real venue.
     `orders` gained a non-terminal `submitted_unconfirmed` status plus
     `broker_order_id` / `broker_status` / `reconciled_at`. The recorded
     quantity is the one actually sent to the broker, never the proposal's;
     if that is somehow unknown, **no row is written** rather than one
     guessing.
  2. **`LiveOrderReconciler` resolves those rows from the broker's own
     answer**, on an interval. It is opt-in
     (`LIVE_ORDER_RECONCILER_ENABLED`, default `false`) and inert without a
     configured live path regardless — it never constructs an adapter and
     never reads a credential, so on the committed configuration every
     cycle short-circuits before touching the database or a broker.
     It never invents a status or a fill. A fill is written only from the
     broker's own `executed_quantity`/`executed_price`; an order the broker
     still reports as working is left completely untouched (never aged out
     or timed out on a clock); an unrecognised broker status counts as
     still-open rather than finished; and a failed broker call skips that
     order for the cycle, because a failure to observe is not evidence
     about the thing observed. `submitted_unconfirmed` → terminal is the
     ONLY update `orders` ever permits, guarded in SQL by
     `WHERE status = 'submitted_unconfirmed'`, so a resolved order can
     never be rewritten and a fill can never be double-written.
  A `broker_closed_unfilled` order (cancelled/expired/rejected **by the
  venue**) is deliberately a different status from `rejected`, which means
  **this system** blocked the trade and it never reached a broker at all.
  Never read the two as interchangeable.

**Standing rule, unchanged by Phases 43 and 49:** building the live path is
not enabling it. `LIVE_TRADING_ENABLED` stays `false` in every default and
every test. Flipping it still requires explicit user approval given in
that moment, per this document's confirmation section above.

## Not yet enforced (because not yet built)

The full audit/decision-chain tables. On the live path specifically: limit
orders, fractional shares (refused outright rather than rounded), and
multi-currency live accounts (one `LIVE_ACCOUNT_CURRENCY`; a missing
balance in it fails the trade rather than substituting another currency).
Partial fills are recorded only once the venue reports the order finished —
`fills` holds one row per order, so a partially-filled order stays under
observation rather than having its in-progress quantity written down. Do
not write code that assumes any of these exist.
