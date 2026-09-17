# Platform Audit — Trading OS (2026-09-17)

A developer + trader review of what the platform does, and the gaps that
matter for the two strategy briefs on the table (the TQQQ options playbook,
and the Elliott-Wave / price-action course materials). Grounded in the
current code at commit `7bcf327`, not from memory.

## What exists and is solid

- **Auth, RBAC, admin** — users, roles, permissions, lockout, password
  reset, admin user/role/broker-grant management.
- **Deterministic risk engine + OMS** — the RISK → PORTFOLIO → BROKER path,
  paper broker, live-broker adapter (inert), reconciliation, emergency
  stop. Live trading is triple-gated and off everywhere.
- **Equity market data** — Longbridge quotes/history/depth + Coinbase
  crypto bars, a symbol-shape router, a real historical bar store with
  paged intraday backfill (Phase 73 fixed the silent 1,000-bar truncation).
- **Strategy Lab** — closed-vocabulary strategy definitions, a persisted
  backtest engine (`engine_v2`), walk-forward, Monte Carlo, robustness,
  universe scan, leaderboard, signal evaluation, deployments with drift
  checks, and an improvement loop that is hold-out validated.
- **Intraday reversal engine (Phase 73)** — sessions, structure
  (swings/sweeps/MSS), a defined-risk bracket simulator with shorts, and
  the §21 metric set.
- **Markets terminal (Phase 74)** — live quote, candles with session
  levels, order book, watchlist.

## Gaps that block the two briefs

### A. Options — the entire subsystem is missing (blocks Task 5)

Confirmed by grep: no option-chain model, no option quotes, no Greeks, no
multi-leg structure, no spread P&L, no options backtest, no multi-leg
order path. The TQQQ options playbook needs all of it. Concretely missing:

1. **Option contract + chain model** and storage.
2. **A pricing/Greeks layer.** Longbridge exposes option quotes/Greeks
   with OPRA permission for *forward* use, but there is **no historical
   option-chain data**, so any *backtest* of a spread must MODEL option
   prices from the underlying (Black-Scholes + an IV assumption) and label
   them as modeled, never as real fills. This is the central honesty
   constraint for Task 5.
3. **Defined-risk structures** — debit/credit verticals, iron condor/fly,
   calendars — with max-loss/max-profit/breakeven math.
4. **Option selection rules** — DTE band, delta band, liquidity/IV filters
   from the playbook §2.
5. **Multi-leg paper execution** and a Longbridge multi-leg order gate
   (native verticals/straddle/strangle/collar per the playbook; iron
   condor/calendar are NOT native and must stay disabled until verified).
6. **Regime router / dual-path** (reversal-debit vs premium-credit).

### B. The intraday engine is not in the product (blocks Tasks 5 & 6)

`intraday_engine.py` has **no route and no persistence** — it is reachable
only from scripts and tests. Its runs are not written to `backtest_runs`
(a bracket trade has several fills; the single-row `backtest_trades` shape
can't hold it), so nothing from Phase 73, and nothing built on top of it,
is runnable or visible from the UI. This is the first thing to fix, because
both briefs' directional setups ride on this engine.

### C. Pattern detection for the course concepts is thin (blocks Task 6)

`structure.py` has swings, liquidity sweeps and market-structure shifts.
The course materials additionally require: Elliott-wave impulse/corrective
counting, ending diagonals, triangles, 3rd-wave setups, order blocks /
fair-value gaps, and the price-action "PAPA" decision logic. None of the
wave/pattern detectors exist yet. Much of this is genuinely discretionary
and only partly mechanizable — the honest deliverable is to encode the
checklist-shaped, rule-based subset and be explicit about the rest.

### D. Cross-cutting gaps

- **No risk-adjusted metrics** — Sharpe/Sortino are computed nowhere
  (verified; `engine_v2` computes max-drawdown only). Both briefs' §14
  backtest requirements list them.
- **No live streaming** — quotes/book are polled every 5s; no websocket.
- **US order-book depth is empty** — the account is LV1; real ladders only
  on LV2 markets (e.g. HK). Not a code gap, an entitlement fact.
- **No general scheduler** — only the snapshot scheduler and the
  deployment runner; no premarket engine, no recurring signal cron.
- **Macro/event calendar filter** (both briefs) — absent.

## Recommended sequencing

1. **Persist + expose the intraday engine** (unblocks B) — new run/trade
   tables that fit multi-fill brackets, a route, and a UI panel.
2. **Task 5 — options foundation:** pricing/Greeks + defined-risk
   structures + selection rules + a model-priced spread backtest that
   rides the existing intraday signals, clearly labeled as modeled. Then
   the live option-chain read and multi-leg paper path.
3. **Task 6 — book-to-skill:** name/summarize the course concepts, encode
   the mechanizable checklist subset (triangle breakout, 3rd-wave, ending
   diagonal, FVG/order-block, PAPA decision rules) as new detectors on the
   intraday engine, and record what is not mechanizable.
4. Cross-cutting: Sharpe/Sortino in the metric layer; event-calendar filter.

Nothing here recommends enabling live trading; all of it is research,
paper, and honest measurement.
