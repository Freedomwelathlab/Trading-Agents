# Trading OS — operator guide

How to use the platform, step by step, against the controls that actually
exist on each page. Where something is not built, this guide says so
rather than describing a feature you will not find.

Production: web `https://web-iota-umber-64epcerqre.vercel.app`, API
`https://trading-agents-production-a6aa.up.railway.app`.

**Safety posture you cannot change from the UI:** `TRADING_MODE=research`
and `LIVE_TRADING_ENABLED=false` on the server. Every order path below is
a **paper** path unless a server operator flips those, sets the
`LONGPORT_LIVE_*` credentials, and (for unattended live) arms the third
switch described in `docs/LIVE_AUTO_TRADING.md`. This guide does not walk
you through arming live trading.

---

## 0. Before anything works: the three server variables

Production shows `NOT_CONFIGURED` for quotes, charts, and the bot until
the Railway service `Trading-Agents` has these variables (Railway →
project → service → **Variables**):

| Variable | Purpose |
|---|---|
| `LONGPORT_APP_KEY`, `LONGPORT_APP_SECRET`, `LONGPORT_ACCESS_TOKEN` | Market data (quotes, bars, depth, news) from your Longbridge **demo/paper** app. All three together or none. |
| `OWNER_BOOTSTRAP_EMAIL` | Your login email. On the next deploy the API makes that account the **owner** (every permission), grants it every broker, and removes `admin:manage` from every other account. Safe to leave set. |
| `JWT_SECRET_KEY` | Rotate to a long random value; the public repo ships a placeholder. |

Railway redeploys on save. After that, load bars once so charts have
history (from your machine, with the API venv):

```bash
python scripts/backfill_prod.py --base-url https://trading-agents-production-a6aa.up.railway.app
```

The alternative to `OWNER_BOOTSTRAP_EMAIL` is running
`scripts/grant_owner.py --email you@example.com --demote-others` with
`DATABASE_URL` pointed at the Railway Postgres; same code, same result.

---

## 1. How to create an account

There is **no public sign-up** — accounts are created by an admin.

1. Sign in as the owner. Left rail → **Administration**.
2. **Users → Create user**: email, password (≥ 8 chars), role. Roles
   are permission bundles; the two the bootstrap creates are `owner`
   (everything) and `trader` (paper trading, strategies, deployments — no
   administration).
3. The new person signs in at `/login`. Password reset: **Users → Issue
   password reset link** produces a one-time link you hand them; the
   `/forgot-password` page needs an email provider configured on the
   server, which production does not have.

To change what a role can do: **Roles → Update role** (the permission
strings are the ones in `apps/api/app/auth/permissions.py`).

## 2. How to add a broker (Longport demo and live)

A "broker" here is a **row the platform routes orders to**, plus the
credentials the server holds for it. Two different things:

**Paper broker (what you will use):**
1. Administration → **Brokers → Create broker**: name (e.g. "Longport
   demo"), provider `paper`, kind **paper**, active.
2. **Broker grants → Grant broker access**: your user id (from the Users
   table) + the broker id (from the Brokers table). A user needs BOTH the
   permission and a grant for that specific broker.
3. Trading desk → **My brokers → Refresh brokers** → **Use**. Every panel
   below is now scoped to it. The paper broker starts with the server's
   default cash (100,000) and fills at the live Longbridge quote.

The paper broker is a simulator inside this platform. It does **not**
place orders on your Longbridge demo account — Longbridge's paper account
and this simulator are two separate books. The Longbridge credentials you
set in step 0 are the **market data** feed for it.

**Live broker (Longport real account):**
1. Same as above but kind **live** with `confirm_live: true` — the UI's
   Create-broker form has the confirmation checkbox.
2. A server operator sets `LONGPORT_LIVE_APP_KEY / _SECRET / _ACCESS_TOKEN`
   (never the same trio as the data feed), `TRADING_MODE=live`,
   `LIVE_TRADING_ENABLED=true`.
3. Your role needs `trade:submit:live`, and every live order needs the
   **Confirm** checkbox on the trade form (`confirm: true` in the request).
4. The **Autotrade bot does not trade live brokers** in this version; it
   refuses them with `skipped_live_not_supported`. Strategy *deployments*
   have a separately-armed live path (`docs/LIVE_AUTO_TRADING.md`).

> Where the credentials go: `.env` locally, Railway Variables in
> production. Nothing in the UI accepts an API key, by design.

## 3. Can I connect other brokers?

**Not today.** The only execution adapters that exist are the internal
paper simulator and the Longbridge live adapter (`apps/api/app/execution/`).
`provider` on a broker row is a label the router does not act on for any
other value. Adding IBKR, Alpaca, etc. means implementing the
`BrokerAdapter` protocol (`execution/broker.py`) — submit, positions,
account state, reconciliation — plus its credential settings; there is no
plug-in that makes it a form fill. Crypto market **data** (Coinbase
public candles) exists for research; no crypto execution adapter exists.

## 4. How to research a ticker

Left rail → **Markets** (or click a symbol in the rail watchlist).

- **Quote** — live price, source, timestamp; polled every 5 s (no
  streaming socket exists).
- **Chart** — this platform's own stored bars (1m…1d) with session levels
  drawn: previous-day high/low/close, premarket high/low, opening range,
  VWAP ±1σ/2σ. An absent level is a dash, never a zero. Bars must be
  ingested first (step 0's backfill or the bot's own refresh).
- **Order book** — Longbridge depth. US symbols on an LV1 account come
  back empty (a vendor entitlement, not a bug); HK symbols show a ladder.
- **Session levels** panel — the numbers behind the chart lines.
- Trading desk → **Quote lookup** for a one-off quote, and the **Agent
  trade** panel gives you the LLM analysts' read (technical, fundamental,
  news) as a *proposal* that still goes through the risk engine.

## 5. How to find opportunity tickers across a pool

Strategy Lab → open a validated strategy → **Universe scan**: paste a
list of symbols, pick a window, run. Every symbol is backtested with that
definition and ranked; the result page shows each symbol's return,
drawdown, trade count and a status (`insufficient_data` is a real answer).
Symbols need stored daily bars (backfill them first).

For intraday, the **Autotrade bot** *is* the scanner: it ranks the
intraday setups across every ticker you gave it every minute and reports
what it found in its cycle rows, even when it takes nothing.

There is no fundamental/valuation screener in the platform.

## 6. How to automate trades during pre-market and the live session

Left rail → **Autotrade**. Fill the form:

| Input | What it does |
|---|---|
| Watchlist + tickers | The pool the bot scans each cycle (snapshot at creation). |
| Market | `regular` 09:30–16:00 ET, `pre_market`, `post_market`, or `auto` (any phase with bars). The bot is **flat by the end of its phase**. |
| Trades in live | Max positions open at once. |
| Trades per day | Hard daily cap on entries. |
| Capital per trade | Sized to the most whole shares this buys; the risk engine may trim. |
| Strategy | `auto` = every setup, with the learning loop demoting losers; `single`/`multi` = exactly what you tick. |
| Minimum score | Each setup scores its evidence 0–5; below this it is skipped. |
| Stop loss | `auto` = the setup's structural stop; `max %` = the tighter of that and your cap. |
| Trailing stop % | Ratchets the stop up behind the highest price seen; never down. |
| Take profit | `auto` = 2R; `min %` = at least that far. |
| Trailing take profit % | After the target is touched, keep running and exit on this giveback from the peak. |
| News blackout | No new entry on a symbol with a headline inside N minutes. |

Then **Create** → the bot is *pending approval* → press **Approve**. From
then on the server runs it every 60 s (`AUTOTRADE_RUNNER_ENABLED`, on by
default): refresh bars from Longbridge → manage every open position's
bracket → scan the rest → open the best signals within the limits → write
a cycle row saying exactly what it did or why it did nothing. **Scan now**
runs one cycle on demand so you can see it work outside market hours
(you will get `skipped_market_closed`, which is the correct answer).

**Pause** takes it out of rotation; **Resume** puts it back; **Stop** is
final and does *not* auto-sell — close leftovers on the desk.

## 7. How to set position size, SL, TP and trailing TP

- **Bot**: the form above. Position size = capital per trade ÷ price,
  floored to whole shares, then capped by the risk engine (10% of equity
  per position, 1% of equity at risk per trade, 50% total exposure).
- **Manual trade** (Trading desk → Submit paper trade): quantity and an
  optional stop price. The risk engine enforces the same caps. There is
  no take-profit or trailing order type on the manual path — exits are
  a SELL you submit.
- **Strategy deployments**: sizing comes from the strategy definition's
  own `position_sizing` block; exits from its exit rule.

## 8. How to find a strategy

Strategy Lab has three sources:

1. **Leaderboard** — every validated strategy ranked by its own
   backtests, with the honest `insufficient_data` / `promising` /
   `validated` / `overfit_risk` status. `validated` means "both checks
   ran and neither flagged", not "this makes money".
2. **Ask the research assistant** (`/strategies/research`) — the LLM
   proposes a *draft* definition in the closed vocabulary; it never
   computes a number and never trades.
3. **The intraday setups** the bot uses: `sweep_mss`, `vwap_reversion`,
   `orb_failure`, `ema_reversal`, `candle_reversal` — from the two
   playbooks and the ASTA course (`docs/ASTA_STRATEGIES.md`).

**What the measurements say (read this):** on 124 sessions of real TQQQ
5-minute data every intraday setup measured negative or insignificant
(`docs/DECISIONS.md` D095: composite t = −3.32; best single setup
`sweep_mss` t = +0.61). TQQQ buy-and-hold over the same window was +51%.
The bot gives you the machinery to run these; it does not make them
profitable. Run it on paper and read its stats before believing anything.

## 9. How to back-test a strategy

1. Strategy Lab → **Create strategy** → builder: indicators, entry rule,
   exit rule, position sizing → **Save draft** → **Validate**.
2. Open the strategy → **Backtests** → **Run backtest**: symbol, window,
   fees/slippage. The run page shows equity curve, drawdown, monthly
   returns, the trade ledger.
3. From the same page: **Walk-forward** (sequential out-of-sample
   windows), **Monte Carlo** (bootstrap of the trade returns), and
   **Robustness** (parameter perturbation). **Backtest history** in the
   rail lists every run.

Bars must exist for the symbol/window (`POST /admin/market-data/backfill`
via the Administration page or `scripts/backfill_prod.py`).

The intraday setups are backtested by `scripts`/`intraday_engine.py`
today, not from the UI (`docs/AUDIT.md` gap B) — the numbers in §8 are
from that engine.

## 10. How to reduce or exit a position

Trading desk → **Submit paper trade** with side **SELL** and the quantity
to close (partial or full), against the same broker. **Portfolio** shows
what you hold (you must supply marks for held symbols — the platform will
not guess a price). The bot's own positions can be closed the same way;
the bot notices the broker is flat and closes its ledger row as
`operator`.

**Everything at once:** Administration → **Emergency stop** halts *every*
order path (manual, agent, deployments, bot) until released. It does not
liquidate.

## 11. How to apply auto-trade

Two mechanisms, both paper unless armed by a server operator:

- **Autotrade bot** (§6) — intraday, multi-ticker, bracket-managed.
- **Strategy deployment** — Strategy Lab → strategy → **Deployments** →
  create (symbols, bar interval, broker) → **Approve**. The deployment
  runner (`STRATEGY_RUNNER_ENABLED`, off by default on the server)
  re-evaluates the definition on daily bars every 5 minutes and trades
  long/flat. Drift detection can auto-pause it.

Both require a human approval action; nothing goes ACTIVE by itself.

## 12. How to grant access to other account holders

1. Administration → **Users → Create user** with a role.
2. **Broker grants → Grant broker access** for each broker they may
   trade. Without a grant the broker does not appear in their **My
   brokers** and every order is a 403.
3. Watchlists and bots are private to their creator; an `admin:manage`
   holder can see and control any bot.
4. **Revoke**: Broker grants → Delete grant; Users → Update user →
   inactive.

---

## Quick reference: what is NOT in the platform

- Public registration; email delivery in production.
- Any broker other than the internal paper simulator and Longbridge live.
- Streaming quotes (everything polls, 5–10 s).
- Short selling on the paper broker (the bot is long-only).
- Multi-leg option execution (option strategies are modeled only —
  `docs/OPTIONS_STRATEGIES.md`).
- A fundamental screener; an economic-calendar filter (the bot's news
  blackout is headline-recency only).
- Intraday backtests from the UI.
