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

- `apps/api/app/deployments/runner.py` — the strategy-deployment runner
  (Phase 63, D081). The first path that places an order with no HTTP
  request and no human in the loop for that specific order. What makes it
  safe:
  1. **A per-strategy trading approval is now enforced, as a state
     machine.** A `strategy_deployments` row is created `pending_approval`;
     the runner's enumeration is `WHERE status = 'active'`; the only
     transition to `active` is an explicit `POST /deployments/{id}/approve`
     gated by a **separate, stricter** permission
     (`strategy:approve_deployment`, distinct from `strategy:deploy`), which
     records who approved and when. There is no code path that produces an
     `active` deployment without that action.
  2. **Opt-in, and `live` places nothing unless deliberately armed.**
     `STRATEGY_RUNNER_ENABLED` defaults `false` — the same fail-closed
     default as the snapshot scheduler, the reconciler and live trading;
     off, the loop never starts. A `mode='paper'` deployment behaves as
     described below.

     A `mode='live'` deployment (Phase 64, D082) can be created and approved
     behind a further, separate `strategy:approve_live_deployment`
     permission. Phase 64 then refused every live cycle unconditionally.
     **Phase 69 (D087) replaced that unconditional refusal with an explicit
     switch** — see the dedicated section below. The refusal branch itself is
     unchanged and is still what every default checkout gets.

     *Why the rule at the top of this document was not weakened.* That rule
     requires explicit user approval for live trading, given in the moment.
     Phase 64 read it as "a scheduler can never satisfy this" and stopped
     there. What the rule actually forbids is live trading that **nobody
     deliberately authorized** — and a person setting
     `STRATEGY_LIVE_AUTO_EXECUTION_ENABLED=true`, alongside two other live
     keys and two capital bounds they had to choose numbers for, in their own
     environment, has authorized it about as explicitly as a configuration
     can express. What that person cannot do is authorize it *per order*, so
     D087 replaces the per-order human check with bounds that do not need one:
     a capital ceiling, a per-trade cap, an open-position ceiling, and two
     loss circuit breakers that stop the robot and hand control back to a
     human. The approval is still explicit; it is given once, over a bounded
     mandate, instead of once per trade.
  3. **Same posture as the paper trade path, not a parallel one.** Each
     actionable signal goes through `submit_trade_and_record` — the one
     sanctioned RISK → PORTFOLIO → BROKER path — against the normal paper
     `RiskLimits` (`require_stop_price=False` per D035: the strategy's exit
     rule is its stop). The **global emergency stop is checked every cycle**
     before any order and halts it (`skipped_emergency_stop`); a UTC-weekend
     gate no-ops it; a per-cycle append-only `strategy_deployment_runs` row
     always resolves to a terminal status; a held position with no bar to
     mark against **fails the cycle visibly** rather than fabricating a
     price. It never invents a fill: a fill is written only by
     `submit_trade_and_record` from the broker's own answer.
  4. **Phase 66 (D084): drift detection runs after every SUCCEEDED cycle,
     always audited, and can only ever make a deployment MORE conservative.**
     `apps/api/app/deployments/drift.py::evaluate_deployment_drift` compares
     this deployment's real win rate (via Phase 65's
     `build_deployment_monitoring`, reused verbatim) to its reference
     backtest's, and `apps/api/app/deployments/runner.py::_check_and_record_drift`
     writes an append-only `strategy_drift_checks` row every single time —
     never only when drift is found — matching `emergency_stop_events`' own
     "a no-op flip still writes a row" audit philosophy. Auto-pause defaults
     OFF (`strategy_drift_auto_pause_enabled=false`): with it off, a
     detected drift is recorded as `action_taken="observed_only"` and the
     deployment keeps running unchanged; only with it explicitly on does the
     system call the EXISTING `pause_deployment()` service function itself.
     This feature never places or sizes a trade differently — it can only
     ever pause a deployment that is already running — so it does not touch
     this document's live-trading gate at all.
  5. **Phase 69 (D087): unattended live execution, armed by one dedicated
     switch and bounded by capital controls.** Operator-facing setup guide:
     `docs/LIVE_AUTO_TRADING.md`. The full gate, in the order
     `apps/api/app/deployments/live_guard.py::evaluate_live_arming` checks
     it — ALL of these, or the cycle resolves to
     `skipped_live_trading_disabled` having read zero rows and contacted
     nothing:

     1. `STRATEGY_LIVE_AUTO_EXECUTION_ENABLED=true` — default `false`.
     2. `TRADING_MODE=live` **and** `LIVE_TRADING_ENABLED=true`.
     3. `STRATEGY_LIVE_TOTAL_CAPITAL` and `STRATEGY_LIVE_CAPITAL_PER_TRADE`
        both set and positive, with per-trade ≤ total. There is deliberately
        no "unlimited" value for either.
     4. The `LONGPORT_LIVE_*` credential trio present, building a real
        `LiveBrokerAdapter`. A missing one is never substituted with a paper
        broker.
     5. The deployment itself `active`, which required
        `strategy:approve_live_deployment` (D082).

     `Settings._enforce_live_auto_execution_is_fully_configured` re-checks
     (1)–(3) at **startup**, so a half-configured robot fails to boot rather
     than discovering its missing bounds at the moment of its first real
     order.

     **Why `STRATEGY_LIVE_AUTO_EXECUTION_ENABLED` is a third key and not a
     reuse of the other two.** Those two arm the *interactive* live path
     (D058), where a human types `confirm: true` per order. Someone enabling
     them to place one live trade by hand must not thereby, silently, also
     start an unsupervised robot. A dedicated test
     (`test_a_live_deployment_is_still_skipped_with_interactive_live_trading_on`)
     holds that line: both D058 keys on, robot switch off, still skipped,
     still zero orders.

     **Capital controls on an armed cycle** (no paper equivalent — a paper
     deployment evaluates none of these). **All of them are scoped PER
     DEPLOYMENT, not per account**: they are computed from one deployment's
     own fills, because `Order.deployment_run_id` is the only clean
     attribution this system has, and nothing traces a position a human
     opened by hand or another deployment owns. N active live deployments
     can therefore commit up to N × `STRATEGY_LIVE_TOTAL_CAPITAL`. An
     account-wide ceiling is future work and is not silently implied by
     these settings — see `docs/LIVE_AUTO_TRADING.md`.
     - `STRATEGY_LIVE_TOTAL_CAPITAL` caps total deployed cost basis,
       recomputed every cycle from this deployment's own real fills, never
       from a remembered running total and never from the whole brokerage
       account.
     - `STRATEGY_LIVE_CAPITAL_PER_TRADE` caps one entry. Applied as a **cap
       on** the strategy's own `position_sizing`, never as a replacement —
       so raising it can never *increase* a conservative strategy's size.
     - `STRATEGY_LIVE_MAX_OPEN_POSITIONS` (default 5) refuses new entries
       when the book is full; exits stay allowed, so positions are never
       trapped open.
     - `STRATEGY_LIVE_MAX_DAILY_LOSS_PCT` (default 3) and
       `STRATEGY_LIVE_MAX_TOTAL_LOSS_PCT` (default 10) are circuit breakers.
       A breach writes `skipped_live_risk_halt` and **pauses** the deployment
       via the existing `pause_deployment()`.
     - Live cycles use D058's tighter `live_risk_*` money limits (5%/20%
       against paper's 10%/50%).

     **A halt never liquidates.** Auto-selling into the event that tripped
     the breaker is how a bad hour becomes a realized loss at the worst
     available price. The positions stay, the robot stops opening new ones,
     and re-arming is a deliberate human action (resume the deployment).
     Asserted by test.

     **An unpriceable position halts the cycle but does not pause the
     deployment.** If a held symbol has no ingested bar, the breakers cannot
     be evaluated honestly, so the robot refuses to trade rather than marking
     the position at cost or assuming no loss. That is an ingestion gap, not
     a loss event, and pausing on it would disguise one as the other.

     **The daily breaker is deliberately conservative.** It sums realized
     P&L from round trips closed today with *all* current unrealized P&L, so
     a multi-day open loss counts against it each day it persists. A strict
     day-over-day measure would need a start-of-day equity snapshot this
     system does not persist for live deployments. Given that choice, a
     control whose job is stopping losses should fire early and say so,
     rather than imply a precision it does not have.

**Standing rule, unchanged by Phases 43, 49 and 69:** building the live path
is not enabling it. `LIVE_TRADING_ENABLED` and, since Phase 69,
`STRATEGY_LIVE_AUTO_EXECUTION_ENABLED` both stay `false` in every default and
every test. Phase 69 built a real unattended live execution path; it did not
turn it on, and nothing in this repository turns it on. Arming it is an act
the operator performs in their own environment, with their own credentials
and their own capital numbers, per this document's confirmation section
above.

**One thing this document cannot give you.** Every control described above
bounds how much the system can lose and how fast it can act. None of them
makes a strategy profitable, and none of them is a view about whether
running a particular strategy on real money is a good idea. A robot that is
correctly bounded and reliably wrong will lose money steadily, within its
limits, exactly as designed. The capital numbers an operator puts in
`STRATEGY_LIVE_TOTAL_CAPITAL` should be money they can afford to lose
entirely.

## Permission matrix

See `docs/PERMISSION_MATRIX.md` (Phase 68, D086) for the complete table of
all 10 `Permission` values, the routes each one gates, and whether an
ADMIN override applies to it (per each permission's own documented
"No ADMIN override" — none of them have one except `admin:manage` itself).
An automated test (`tests/auth/test_permission_matrix.py`) walks the real
constructed app and asserts every non-public route requires SOME
authentication dependency — a regression guard against a future route
shipped with none, not a claim that the permission checked on any given
route is the correct one.

## Not yet enforced (because not yet built)

The full audit/decision-chain tables. On the live path specifically: limit
orders, fractional shares (refused outright rather than rounded), and
multi-currency live accounts (one `LIVE_ACCOUNT_CURRENCY`; a missing
balance in it fails the trade rather than substituting another currency).
Partial fills are recorded only once the venue reports the order finished —
`fills` holds one row per order, so a partially-filled order stays under
observation rather than having its in-progress quantity written down. Do
not write code that assumes any of these exist.
