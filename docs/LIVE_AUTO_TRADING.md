# Arming unattended live trading

Phase 69 (D087). This is the operator's guide to turning on real,
unattended live execution — the runner placing real orders with real money
on a timer, with no per-trade confirmation.

**Read `docs/TRADING_SAFETY.md` first.** This document tells you which
switches to flip. That one tells you what the switches actually protect you
from, and what they do not.

---

## What you are turning on

A scheduled loop that, every `STRATEGY_RUNNER_INTERVAL_SECONDS`, evaluates
each `active` `mode='live'` deployment against fresh bars and places real
buy and sell orders through your real Longbridge account, within bounds you
set once.

There is no per-order confirmation. That is the point, and it is the risk.
The controls below bound how much can be lost and how fast; none of them
makes a strategy correct. A well-bounded strategy that is reliably wrong
will lose money steadily, within its limits, exactly as designed.

**Only commit capital you can afford to lose entirely.**

---

## The five gates

A live cycle places an order only if ALL of these hold. Miss any one and
the cycle resolves to `skipped_live_trading_disabled` having read zero rows
and contacted no broker; `run.error_detail` names the gate that said no.

| # | Gate | Where |
|---|------|-------|
| 1 | `STRATEGY_LIVE_AUTO_EXECUTION_ENABLED=true` | `.env` |
| 2 | `TRADING_MODE=live` **and** `LIVE_TRADING_ENABLED=true` | `.env` |
| 3 | Both capital bounds set and positive, per-trade ≤ total | `.env` |
| 4 | `LONGPORT_LIVE_*` credential trio present | `.env` |
| 5 | The deployment `active` via `strategy:approve_live_deployment` | API |

Gate 1 is separate from gate 2 on purpose: enabling live trading so you can
place one confirmed order by hand must not also start a robot. Flipping
gate 2 alone leaves live deployments skipped, and there is a test that
holds that line.

Gates 1–3 are re-checked at **startup**. A half-configured robot fails to
boot rather than discovering a missing bound mid-flight.

---

## Configuration

```bash
# --- gate 1: the robot switch -------------------------------------------
STRATEGY_LIVE_AUTO_EXECUTION_ENABLED=true

# --- gate 2: the interactive live path must also be on ------------------
TRADING_MODE=live
LIVE_TRADING_ENABLED=true

# --- gate 3: your capital mandate (no defaults, no "unlimited") ---------
STRATEGY_LIVE_TOTAL_CAPITAL=10000       # most deployed at once
STRATEGY_LIVE_CAPITAL_PER_TRADE=1000    # most committed by one entry

# --- circuit breakers (these have defaults; tune them deliberately) -----
STRATEGY_LIVE_MAX_OPEN_POSITIONS=5
STRATEGY_LIVE_MAX_DAILY_LOSS_PCT=3      # % of total capital, one UTC day
STRATEGY_LIVE_MAX_TOTAL_LOSS_PCT=10     # % of total capital, cumulative

# --- the runner loop itself ---------------------------------------------
STRATEGY_RUNNER_ENABLED=true
```

### Which account gets traded

Your `LONGPORT_LIVE_*` credentials do, and **only** they. The `Broker` row a
deployment points at supplies the `kind=live` check and labels the orders in
this system's own records; it does not select a brokerage account. Two live
`Broker` rows on one server both trade whichever account the credentials
name. (This is inherited from the interactive live path, D058 — not new in
Phase 69 — but it is worth knowing before you arm a robot.)

Gate 4's credentials are **not** listed here deliberately. Never paste a
Longbridge token into a chat, an issue, a commit, or a log — put it
directly into your local `.env`, which is gitignored. The live trading
credentials are separate settings from the read-only quote credentials, and
a quote key must never be reused as a trading key.

---

## What the numbers mean

**`STRATEGY_LIVE_TOTAL_CAPITAL`** — the ceiling on deployed cost basis, not
a starting balance and not a share of your account. Your account may hold
capital earmarked for other things; this is how you say how much the robot
may touch. Recomputed every cycle from the robot's own real fills, never
from the whole account and never from a remembered total.

> ### ⚠ This bound is PER DEPLOYMENT, not per account
>
> This number, and both loss breakers, are evaluated against **one
> deployment's** own positions and P&L. Two active live deployments can each
> commit up to this amount — `STRATEGY_LIVE_TOTAL_CAPITAL=10000` with three
> live deployments running means **up to $30,000** deployed, not $10,000.
>
> The reason is attribution: `Order.deployment_run_id` traces an order to
> the deployment that placed it, and nothing traces a position you opened by
> hand or that another deployment owns. Summing across deployments would
> require guessing at that, and this system does not guess about money.
>
> **Run one live deployment**, and this number means exactly what it says.
> If you run several, do the multiplication yourself and set each one's
> share. An account-wide ceiling is genuine future work.

**`STRATEGY_LIVE_CAPITAL_PER_TRADE`** — a **cap** on what one entry may
commit, applied on top of the strategy's own position sizing. A strategy
that wants less gets what it wants; one that wants more is trimmed and
floored to whole shares. Raising this can never increase a conservative
strategy's size.

**The two loss breakers** halt the runner and **pause** the deployment.
They do **not** sell anything — auto-liquidating into the event that
tripped the breaker is how a bad hour becomes a realized loss at the worst
price available. Your positions stay; the robot stops opening new ones.

The daily breaker is deliberately conservative: it counts today's realized
P&L plus *all* current unrealized P&L, so a multi-day open loss counts
against it every day it persists. It fires earlier than a strict
day-over-day measure would. That is the intended bias for a control whose
job is stopping losses.

---

## Operating it

**Watch these.** A `strategy_deployment_runs` row is written every cycle:

| Status | Meaning |
|---|---|
| `succeeded` | Cycle ran; check its counters for what it did |
| `skipped_live_trading_disabled` | Not armed — `error_detail` says which gate |
| `skipped_live_risk_halt` | **A breaker fired.** Real money stopped. Look now |
| `skipped_not_active` | Paused or stopped — including after a halt |
| `failed` | Something broke; `error_detail` has it |

`GET /deployments/{id}/monitoring` shows real vs. backtest performance.
`GET /deployments/{id}/drift-checks` shows whether live behaviour has
diverged from the backtest.

**To stop everything immediately**, at any time:

```
POST /admin/emergency-stop
```

The global emergency stop is checked every cycle, before any order. It halts
paper and live alike, needs no restart, and is persisted — so it survives one.
Clear it with `POST /admin/emergency-stop/deactivate`; read it with
`GET /admin/emergency-stop`.

**To stop one deployment**: `POST /deployments/{id}/pause`.

**After a breaker fires**, the deployment is paused and stays paused. Work
out what happened before resuming — a breaker that gets reflexively resumed
is not a breaker. Resume with `POST /deployments/{id}/resume`.

---

## Turning it off

Set `STRATEGY_LIVE_AUTO_EXECUTION_ENABLED=false` and restart. Live
deployments immediately go back to `skipped_live_trading_disabled` every
cycle. Existing positions are untouched — the robot stops trading them, it
does not close them.
