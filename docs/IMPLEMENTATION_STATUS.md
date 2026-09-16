# Implementation Status

Update this after meaningful implementation work — not for every commit.

## Completed

**Phases 53-68 (D070-D086) completed the Strategy Lab initiative. Phase 69
(D087) then built the unattended live execution path those phases had
deliberately left out. Phase 70 (D088) opened the BTC/USD strategy work
with a vertical slice: transaction-cost modelling, two more indicators, an
intraday bar vocabulary, and a chart that draws this platform's own signals
on its own bars.**

`LIVE_TRADING_ENABLED` and `STRATEGY_LIVE_AUTO_EXECUTION_ENABLED` both stay
`false` on every default and every test in this repository. Phase 69 built
the path; it did not turn it on, and nothing here turns it on — arming is an
act the operator performs in their own environment, with their own
credentials and their own capital numbers. On any default checkout a
`live`-mode deployment still resolves every cycle to
`skipped_live_trading_disabled` having read zero rows and contacted no
broker, exactly as under D082.

Nothing below claims the platform is "production ready" or "fully secure"
beyond what each phase actually verified, and nothing below is a claim that
any strategy makes money.

- Phase 73: an intraday engine for the TQQQ reversal playbook, and a
  negative research result (2026-09-16, D091). The brief supplied a
  26-section playbook and asked for its best strategies on TQQQ, both
  directions, with real risk management and testing. **Almost none of it
  was expressible**: a `StrategyVersion` is indicators plus one entry rule,
  one exit rule and a sizing fraction, evaluated long-only, while the
  playbook's priority-10 setup needs a swept previous-day low, a reclaim,
  a market-structure shift, a retest, an ATR-buffered structural stop,
  1R/2R partials with a trailed runner, a short side and a 15-minute
  regime filter beside 5-minute execution. So `intraday_engine.py` stands
  BESIDE `engine.py` and `engine_v2.py`, neither replacing nor modifying
  them, reusing `CostModel` and the new `sessions.py` / `structure.py`.

  **Longbridge was silently returning 7% of every intraday request.**
  `history_candlesticks_by_date` caps at 1,000 candles and serves the most
  RECENT ones regardless of `start`. For daily bars that ceiling is ~4
  years and never bit; for 5-minute bars it is ~13 trading days, so a
  180-day request returned 13 of 124 sessions with **no error and a job
  row recording SUCCEEDED**. Backward paging via
  `history_candlesticks_by_offset` turns the same request into **23,808
  bars over 124 sessions**.

  **The vendor's timestamps are local time, and the container hid it.**
  The SDK converts epochs to the running process's timezone and returns a
  naive datetime; `_as_utc` asserted that reading was UTC. True on a UTC
  host - which the API container is - and eight hours wrong on the UTC+8
  development machine, where it placed the 09:30 ET open at 21:30 UTC.
  Daily bars absorbed it (midnight-ET timestamps, so only the instant
  moved); intraday bars, where the session boundary IS the information,
  were corrupted. Established by measurement: the only gap in a run of
  regular-hours bars is the 17.5-hour overnight one, and the naive session
  start shifts 21:30 -> 22:30 exactly at the US DST boundary, which can
  only happen if the source zone is fixed-offset.

  **Two defects that made the first real results meaningless**, both found
  by reading a result that did not add up rather than by a test. R was
  measured from the pre-cost signal price while P&L used the post-cost
  fill - an error that scales inversely with stop distance, so 37 of 239
  trades lost more than a full stop and one reported **-169R**. And there
  was no stop-quality test, so setups with a stop distance of 0.00% of
  price were being taken, where the spread decides the outcome. A third,
  separate gap was fidelity rather than correctness: entering at the
  structure shift instead of at the playbook's retest put the entry at the
  top of the move with the stop still at the swept extreme, giving 15%
  TP1 against 76% stopped - an artefact of entry location, not a fact
  about the market. With the retest: 26% and 66%.

  **What the data said, on 124 real sessions with costs, both directions,
  flat by the bell.** `sweep_mss` - the playbook's own "best single setup"
  - is the only one that did not lose money: 116 trades, 55.2% win rate,
  expectancy **+0.051R**, profit factor 1.13. **It is not statistically
  significant** (t = +0.61); out-of-sample is slightly better than
  in-sample (+0.099R vs +0.023R), which argues against overfitting but
  does not create an edge. The other three setups all lost money, and the
  composite of all four is significantly NEGATIVE (321 trades, **t =
  -3.32**, PF 0.66) - the one result here that clears significance clears
  it in the wrong direction. Section 14's scoring gate degrades results
  monotonically and its proposed 8/10 threshold admits **zero trades in
  six months**. Against buy-and-hold it is not close: TQQQ ran **+51.36%**
  over the window; the best strategy returns +2.96% of equity at 0.5%
  risk per trade, the composite -30.43%. **Nothing is deployable and
  nothing was deployed.**

  Not TQQQ-specific: every level derives from the instrument's own
  sessions and every distance is in ATR units, so the same config runs
  against any symbol with intraday bars. New modules: `marketdata/ohlcv.py`
  (one shared bar Protocol, replacing three local duck types),
  `marketdata/sessions.py`, `marketdata/structure.py`,
  `backtesting/brackets.py`, `backtesting/setups.py`,
  `backtesting/intraday_engine.py`, `backtesting/intraday_metrics.py`.
  `tzdata` is now a declared dependency under a `sys_platform == 'win32'`
  marker - `zoneinfo` has no bundled database on Windows and every session
  boundary raises without it.

- Phases 71-72: real BTC/USD data, and an improvement loop built not to
  fool itself (2026-09-15, D089/D090). **Longbridge cannot price spot BTC**
  - `BTCUSD.BKKT` answers `301600 invalid symbol` while live AAPL quotes
  and 5m candles work on the same credentials, so it is an entitlement
  fact, not a token one; the account can price the BTC ETFs (IBIT, GBTC,
  BITO, MSTR) and not Bitcoin. New `CoinbaseBarProvider` (public API, no
  credentials) supplies genuine BTC/USD, chosen after measuring the
  alternatives: Kraken's OHLC ignores `since` backwards so hourly reaches
  only ~30 days, and Binance's real `BTCUSD` trades 0.04 BTC/hour against
  BTCUSDT's 560. `BarBackfillRouter` dispatches on symbol shape because the
  two vendors' universes are DISJOINT - an unroutable symbol is refused,
  never tried against every provider. **5,166 hourly + 17,775 15-minute
  real BTC/USD bars ingested.**
  **Three limits that each produced a plausible wrong answer**, all found
  by pointing real intraday crypto data at the engine: whole-unit sizing
  meant $10,000 of a $70,000 asset floored to ZERO, so every BTC backtest
  returned "0 trades, 0.00%" - indistinguishable from a strategy that never
  fired (fixed: fractional precision for crypto, whole units for equities,
  decided per symbol); `backtest_equity_points` was UNIQUE on
  `(run_id, date)` so 24 hourly points collided and the write died AFTER
  the replay finished (migration `0029` re-keys on the bar's timestamp and
  adds intraday trade timestamps); and `upsert_bars` built one INSERT,
  hitting Postgres' 32,767 bind-parameter ceiling at ~3,640 bars (now
  chunked at a size derived from the column count). Separately measured and
  recorded: **the Risk Engine caps any single order at 10% of equity**, so
  `all_in` and any fraction above ~0.09 fill nothing and report an empty
  backtest with no error.
  **The platform's first real results.** Five conventional strategies over
  180 days of real hourly BTC/USD, costed: every one underperformed
  buy-and-hold (+8.29%) and three lost money; best was sma-50-200 at
  +1.69%. `rsi-30-70` won **59.3% of its trades and still lost money** -
  $144.84 of costs across 54 round trips - which before D088's cost model
  would have reported a profit.
  **Backtest history** (`GET /backtest-runs` + `/strategies/history`): every
  run across all strategies, ownership enforced in the JOIN, failed runs
  included deliberately.
  **The improvement loop** (D090) is a search that cannot quietly overfit:
  train and validate are separate NOT NULL columns so "improve against
  everything" is inexpressible; the hold-out is touched once per iteration
  and only for a candidate that already won on train; acceptance needs both
  windows; and the search stops at the first rejection rather than spending
  more of the hold-out. On its first real run it rejected its own best
  candidate - EMA(50)->EMA(55) improved train (-0.36% -> -0.22%) and
  degraded held-out (1.98% -> 1.91%) - and left the baseline standing. A
  latent bug surfaced with it: `perturbation.py` wrote
  `position_sizing.fraction` as a Decimal, unnoticed because `robustness.py`
  never persists a variant.
  Verified 2026-09-15 at migration head `0030`: **1276 backend tests**
  (1265 -> 1276, +11), **325 frontend tests**, ruff / mypy (146 files) /
  eslint (0 errors, down from 10 pre-existing) / `tsc` / secret scan all
  clean.
  **A wrong fix caught by the suite, recorded because it nearly shipped.**
  The improvement loop first failed on "Object of type Decimal is not JSON
  serializable"; the obvious repair - have `perturbation.py` write a float
  - was wrong, and `test_no_returned_value_is_ever_a_binary_float` failed
  on it. That module computes an EXACT Decimal deliberately, so 0.25*0.9 is
  precisely 0.225 rather than 0.225000000000000005..., and a float
  reintroduces the artifact the test forbids. The conversion belongs at the
  DATABASE boundary and now happens only there (`_json_native`), leaving
  `perturbation.py` untouched at 22/22.
  No BTC strategy has been deployed to paper: nothing here has earned it.

- Phase 70: transaction costs, EMA/ATR, an intraday bar vocabulary and a
  signal chart (2026-09-15, D088) — the first vertical slice of the BTC/USD
  strategy work: one change at each layer, chosen so the whole pipeline is
  exercised rather than one layer built out ahead of the rest.
  **The headline is the cost model.** Before this phase a case-insensitive
  grep for `slippage`/`commission`/`fee` across the entire
  `apps/api/app/backtesting/` package returned **zero matches** — every
  backtest figure this platform had ever produced described a frictionless
  market, distorted in the flattering direction, and a round trip that broke
  even graded as neither a win nor a loss when it was in fact a loss. New
  `backtesting/costs.py` applies a fee and a slippage allowance (10 bps /
  5 bps by default, deliberately non-zero so the most optimistic assumption
  is never the one nobody chose) to every simulated fill as an adverse
  adjustment to the execution PRICE — which is an exact identity with
  slipping the price and deducting a fee separately, asserted over five
  price scales at exact Decimal equality. Sizing is computed against the
  costed price, without which an `all_in` entry would propose a notional
  larger than its cash and the paper broker would refuse the order outright.
  `cost_model` is a REQUIRED keyword on `run_strategy_backtest`:
  `CostModel.frictionless()` exists and is legitimate, but must never be
  reachable by forgetting. Migration `0028` records `fee_bps`,
  `slippage_bps`, `total_fees`, `total_slippage` on every run, all nullable
  with no back-fill — NULL means "predates cost modelling, costs unknown",
  which is a different claim from zero, and the UI renders it as such.
  **Two indicators**, chosen rather than accumulated: EMA (the spec's trend
  core, seeded from the SMA so the value does not depend on where the caller
  started the series) and ATR — which is what makes a stop-loss possible at
  all, the prerequisite the spec's SL/TP and volatility sizing need, not a
  fourth moving average. ATR is the first indicator reading `high`/`low`,
  both nullable, and it refuses a close-only bar rather than estimating a
  day's range from its close.
  **`bar_interval` becomes one shared closed `BarInterval` vocabulary**
  (`1m`/`5m`/`15m`/`30m`/`1h`/`1d`) replacing six separate `Literal["1d"]`s.
  Their stated reason — that an unsupported interval would "silently find no
  bars" — is now handled properly by a persisted FAILED run naming the
  missing range, which says more than a 422 on the interval did. The
  Longbridge adapter maps each interval to the SDK's `Period` by attribute
  name through `getattr` (keeping the vendor import lazy) and **raises** on
  an unmappable one: falling back to `Period.Day` would persist daily bars
  under a `bar_interval` of `"5m"` and nothing downstream could detect it.
  **A TradingView-style chart of our own bars**, via `lightweight-charts`
  (Apache-2.0, by TradingView) — not TradingView's free embed widget, which
  renders TradingView's own data in a sealed iframe and cannot take a bar
  series or draw a marker. That is a correctness problem, not just an API
  limit: a marker on someone else's bars would sit on a bar the backtest
  never saw. New `GET /market-data/{symbol}/bars` reads through the same
  `MarketDataStore` the engine replays against, never contacts a vendor and
  never ingests (an un-backfilled symbol is an empty 200, not a 404), and
  refuses rather than truncates above 5,000 bars. The component skips a
  close-only bar rather than inventing a candle from its close and discloses
  the count, and drops a signal whose bar is not in the window rather than
  snapping it to the nearest one.
  **A test-hygiene finding:** four existing tests used `ema` as their
  example of an *unknown* indicator type, so implementing EMA made all four
  pass for the wrong reason — nothing raised because nothing was unknown any
  more. Each now uses a name nobody is likely to implement, with a comment
  saying why.
  **A near-miss caught by the suite:** the jsdom canvas stub
  `lightweight-charts` needs was first added to the shared `test/setup.ts`,
  where it broke five unrelated suites — Recharts measures text through
  `ctx.measureText(...).width`, and a global stub answering every method
  with `undefined` timed out every Recharts chart test. It now lives beside
  the one component that needs it.
  **Not verified, and stated as such:** only the `Day` vendor mapping has
  ever made a real round trip. The five intraday mappings are correct by
  direct introspection of the installed SDK's `Period` enum and are tested
  against a stub client — which proves the adapter asks for the right
  period, not that Longbridge returns intraday candles for any given symbol
  or entitlement. The token available during this phase was expired
  (`401103 token is expired`), so no live intraday call was possible. No
  BTC/USD strategy has been authored, backtested or deployed by this phase;
  it built the capabilities that work needs.
  Verified 2026-09-15 at migration head `0028`: **1265 backend tests**
  (1215 → 1265, +50), **325 frontend tests / 41 files** (312 → 325), ruff
  clean, mypy clean across 143 source files, `tsc --noEmit` clean, secret
  scan clean. `eslint` reports 10 errors, all of them PRE-EXISTING
  (`react-hooks` "setState synchronously within an effect") in ten files
  this phase does not touch; every file it adds or changes is clean, and
  the one file it modifies carried its single warning at the same line
  before the change. Fixing the pre-existing ten is real work that belongs
  to its own change, not to this one. Both live switches `false` throughout; no live
  credential handled and no live order placed at any point.
  The full backend run reported one failure,
  `test_the_identical_cycle_on_a_weekday_does_capture_a_real_snapshot`,
  which was **database pollution rather than a regression** - the same
  failure mode D087's closing note already records. Three orphaned `brokers`
  rows left by earlier aborted teardowns held positions in symbols that test
  does not own, and it asserts a GLOBAL `provider.calls` list, so the
  scheduler's cycle priced those symbols too. Re-run against a cleaned
  database: 6/6 pass. Nothing in this phase touches the snapshot scheduler.

- Phase 69: unattended live execution (2026-09-11, D087) — **supersedes the
  operative half of D082**. A `mode='live'` deployment's scheduled runner can
  now place real orders with real money, gated behind a THIRD dedicated
  switch (`STRATEGY_LIVE_AUTO_EXECUTION_ENABLED`, default `false`) that is
  deliberately separate from D058's `TRADING_MODE=live` /
  `LIVE_TRADING_ENABLED` pair — so enabling the interactive live path to
  place one confirmed order by hand does NOT also start an unsupervised
  robot. D082's own test survives, rewritten, asserting exactly that. New
  `deployments/live_guard.py` answers two questions in a fixed order: is the
  robot armed (pure configuration, zero I/O, the cheap gate that protects
  every default checkout), and do this deployment's real positions still fit
  the operator's capital bounds. Bounds: `STRATEGY_LIVE_TOTAL_CAPITAL` and
  `STRATEGY_LIVE_CAPITAL_PER_TRADE` (no defaults, no "unlimited" value, and
  the app refuses to boot armed-without-them), an open-position ceiling, and
  daily/total loss circuit breakers that write the new
  `skipped_live_risk_halt` status and PAUSE the deployment through the
  existing `pause_deployment()` — **never liquidating**, because auto-selling
  into the event that tripped the breaker is how a bad hour becomes a
  realized loss. The per-trade number is a CAP on the strategy's own sizing,
  not an override, so raising it can never increase a conservative strategy's
  position. Every figure is recomputed each cycle from this deployment's own
  real fills, never a remembered total and never the whole brokerage account.
  Live cycles use D058's tighter `live_risk_*` limits. Migration `0027` adds
  the one new enum label. **A near-miss caught by an existing paper test:**
  the first draft swapped the runner's risk-limit builder wholesale, which
  would have re-enabled `require_stop_price` and made the Risk Engine reject
  every automated entry, paper and live alike — see D087's closing note.
  Verified 2026-09-11 on a fresh isolated Postgres/Redis at migration head
  `0027`: **1215 backend tests** (1178 → 1215, +37), **312 frontend tests /
  39 files** (311 → 312), `npm run build` clean, ruff clean, mypy clean
  across **144 source files** (143 → 144), secret scan clean, full backend
  run 9m11s exit 0. Both live switches `false` throughout; no live
  credential handled and no live order placed at any point.

- Phase 68: final hardening (2026-09-11, D086) — pure verification, no new
  API surface, no schema change, no user-facing capability; the last phase
  of the Strategy Lab initiative. **Fuzz-testing**
  (`tests/strategies/test_fuzz.py`) found and fixed THREE real crash bugs:
  `validate_definition` raised `TypeError` on an unhashable vocabulary
  value (`{"op": {...}}` instead of a string) instead of returning an
  itemized error — fixed with a new `_in_vocabulary()` helper used at all
  three vocabulary-check sites in `strategies/validation.py`; the evaluator
  (`strategies/expressions.py`) raised `TypeError` on a `None` bar close
  and `decimal.InvalidOperation` on a Decimal NaN close instead of failing
  closed with `None` — both now caught and answered with the module's
  existing `None` semantics. A static AST guard also now runs against
  `validation.py`/`expressions.py`/`perturbation.py`/`engine_v2.py` proving
  none of them ever calls `eval`/`exec`/`compile`/`__import__` or imports by
  name inside a function body. **A permission-matrix audit**
  (`tests/auth/test_permission_matrix.py`) mechanically walks the real
  constructed FastAPI app and proves every non-public route requires SOME
  authentication dependency (not a claim about which permission is
  correct — each route's own tests already prove that); found zero routes
  missing auth, but did surface one stale docstring
  (`Permission.SUBMIT_LIVE_TRADE` claimed "not enforced anywhere yet" —
  false since Phase 43/D058) while the new `docs/PERMISSION_MATRIX.md`
  table was being cross-checked, now fixed. **One real capped-load test**
  (`tests/backtesting/test_universe_scan_load.py`) runs a real universe
  scan at `MAX_SCAN_SYMBOLS` (50) real seeded symbols against real
  Postgres through the real HTTP API, asserting it completes within a
  generous 60-second bound — explicitly NOT a concurrency benchmark (this
  codebase has no task queue to load-test concurrently).
  **Final regression: 1178 backend tests** (1109→1178, +69 across the three
  new test files above), `ruff check apps tests migrations` clean, `mypy
  apps` clean (143 files, unchanged), `bash scripts/secret_scan.sh` clean,
  full run 23m51s on one fresh isolated Postgres/Redis. **311 frontend
  tests / 39 files, unchanged** (no frontend file touched). No new
  migration (schema unchanged, still at `0026`). No new dependency (no
  `hypothesis` added — a hand-crafted 42-case adversarial table per the
  phase's own instruction).

- Phase 67: AI strategy research assistant (2026-09-11, D085) — advisory-only
  agent (`apps/api/app/agents/strategy_research_assistant.py::
  StrategyResearchAssistant`) that proposes a candidate `StrategyDefinition`
  draft plus a plain-English rationale from a short natural-language brief
  (e.g. "a mean-reversion idea using RSI"). No order/trade-submission path at
  all, same as every analyst (spec §62, `docs/AGENT_POLICY.md`). Every
  proposed definition is run through the SAME structural validator a
  manually-authored strategy must pass
  (`apps/api/app/strategies/validation.py::validate_definition`, called
  verbatim) — an invalid draft is a normal 200 result with itemized
  `validation_errors`, never a fabricated pass and never an exception; only
  an unparseable/malformed LLM response raises `AnalystOutputError` (502 at
  the route). New route `POST /strategies/research/propose`, same
  `strategy:manage` permission as every other `/strategies` route — no new
  permission — and NO database write of any kind: the caller reviews the
  draft and creates it for real through the existing `POST /strategies` +
  `POST .../versions/{id}/validate` path if they choose to.
  **1109 backend tests** (1097→1109, +12: 7 `test_strategy_research_assistant.py`,
  5 `test_strategies.py`); `ruff`, `mypy apps` (143 files, up from 142),
  `secret_scan` clean. **311 frontend tests / 39 files** (303/38→311/39,
  +8), `npm run build` clean. No new dependency, no migration.

- Phase 66: drift detection (2026-09-11, D084) — strategy-scoped auto-pause
  when a deployment's real, closed-trade performance drifts from its own
  backtest expectation. Reuses Phase 65's `build_deployment_monitoring`
  verbatim (`deployments/drift.py::evaluate_deployment_drift`) to compare
  actual vs. reference-backtest win rate; `INSUFFICIENT_DATA` when no
  reference backtest exists or too few round trips
  (`strategy_drift_min_round_trips`, default 10); `DRIFT_DETECTED` when the
  absolute win-rate deviation exceeds `strategy_drift_max_win_rate_deviation_pct`
  (default 30 points, deliberately generous). Runs once per SUCCEEDED
  runner cycle, in the SAME transaction (no new scheduler/lock) — never for
  FAILED/SKIPPED_*/live-mode cycles. New append-only `strategy_drift_checks`
  table (migration 0026) writes a row EVERY cycle, never only when drift is
  found (`emergency_stop_events`' own audit philosophy). Auto-pause defaults
  OFF (`strategy_drift_auto_pause_enabled=false`, fail-closed like every
  other automation here); when on, calls the EXISTING `pause_deployment()`
  verbatim — `action_taken` records `"none"` / `"observed_only"` /
  `"paused"`. New route `GET /deployments/{id}/drift-checks`, same
  `strategy:deploy` permission and ownership check as the sibling list
  routes — no new permission, no new scheduler.
  **1097 backend tests** (1089→1097, +8: 4 `test_drift.py`, 3
  `test_runner.py`, 1 `test_deployments.py`); `ruff`, `mypy apps` (142
  files, up from 141), `secret_scan` clean. Migration `0026` round-trips
  clean. **303 frontend tests / 38 files** (296/37→303/38, +7: 5
  `DeploymentDriftTable.test.tsx`, 2 `DeploymentList.test.tsx`), `npm run
  build` clean. No new dependency.
- Phase 65: strategy monitoring (2026-09-11, D083) — read-only actual vs.
  expected performance for one `StrategyDeployment`. **Places no orders,
  writes nothing** — a pure read joining two already-honest sources: this
  deployment's own real fills, and its strategy version's own latest real
  backtest.
  **(1) `actual` is scoped to THIS deployment's own orders** — joined
  `orders` → `strategy_deployment_runs` filtered to `deployment_id`, never
  the whole broker account (which can hold another deployment's or a
  human's trades).
  **(2) Round trips use the FILL's price/time, never
  `Order.estimated_price`** — `_build_round_trips` walks `(Order, Fill)`
  pairs maintaining one open lot per symbol from a BUY fill, closing it on
  the next SELL fill in that symbol; a BUY-while-open or SELL-while-flat
  (should never happen given this runner's long/flat discipline) is
  skipped, never merged into an existing lot.
  **(3) The reference backtest is `_latest_succeeded_backtest`, reused
  verbatim from `strategies/scoring.py`** (D072 precedent) — the same run
  the Phase 59 leaderboard calls this version's headline backtest.
  `expected.status` is `"no_reference_backtest"` (all other fields `null`)
  or `"available"` with the run's numbers copied through. Known scope
  limit: a deployment can trade several symbols but the reference backtest
  covers exactly one (`expected.symbol` names it) — not glossed over.
  **(4) `win_rate_pct`/`avg_return_pct` are `null`, never a fabricated 0%
  or 100%, at zero round trips**; `total_realized_pnl` is `0` (a true sum,
  not a rate).
  New route `GET /deployments/{id}/monitoring` on the existing
  `deployments_router`, same `strategy:deploy` permission and ownership
  check as `.../runs`/`.../signals` — no new permission, no migration.
  **1089 backend tests** (1078→1089, +11: 10 in `test_monitoring.py`, 1 in
  `test_deployments.py`); `ruff`, `mypy apps` (141 files, up from 140),
  `secret_scan` clean. **296 frontend tests / 37 files** (292/36→296/37,
  +4), `npm run build` clean. No new dependency.
- Phase 64: broker abstraction + controlled live execution — SCAFFOLDING
  ONLY (2026-09-11, D082) — `mode` widens to `"paper" | "live"` at the
  schema and service layer, symmetrically validated (a `live` deployment
  needs a LIVE broker, `NOT_A_LIVE_BROKER` otherwise, mirroring the existing
  `NOT_A_PAPER_BROKER`). **The runner never places a live order.** A new
  `StrategyDeploymentRunStatus.SKIPPED_LIVE_TRADING_DISABLED` is written,
  unconditionally, for every cycle of every `live` deployment — before
  touching market data, the broker, or the risk engine, and without
  consulting `TRADING_MODE` / `LIVE_TRADING_ENABLED` at all, so it cannot be
  flipped on by an environment variable. Proven by test with a settings
  object where that gate is nominally wide open.
  **(1) Why not reuse Phase 43/D058's live-trade gate** — that gate assumes
  a human supplying `confirm=true` on one interactive HTTP request; a
  scheduled cycle has no per-trade human, and this project's rule requires
  in-the-moment approval for every live trade. Rather than fake a
  per-request confirmation for an unattended loop, the runner refuses live
  execution outright until a future, separately-approved phase builds a
  mechanism actually suited to it.
  **(2) New permission `strategy:approve_live_deployment`** — required IN
  ADDITION to `strategy:approve_deployment` to approve a `live` deployment
  specifically (checked in the route handler once the deployment's mode is
  known). Holding it approves the row; it does not make anything trade.
  **(3) A live deployment is still enumerated and visibly skipped**, not
  filtered out of the runner's query — an explicit, audited
  `SKIPPED_LIVE_TRADING_DISABLED` every cycle beats silent invisibility.
  **(4) Migration `0025`** — one new Postgres enum label on
  `strategydeploymentrunstatus` (`ALTER TYPE ... ADD VALUE`, D014's
  precedent); no schema change for `mode` (already `VARCHAR(8)`). Round-trips
  clean. **(5) No frontend change** — `CreateDeploymentForm.tsx` still only
  POSTs `mode: "paper"`; a `live` row (created via the API) would render
  correctly since `DeploymentList.tsx` already shows `mode` raw, but the web
  app offers no way to create one, deliberately.
  **1078 backend tests** (1071→1078, +7: 3 service, 1 runner, 3 API);
  `ruff`, `mypy apps` (140 files, unchanged), `secret_scan` clean. Frontend
  unchanged — no frontend file touched.
  Not built (deliberately, this phase): any way for the runner to actually
  place a live order; a per-cycle or per-strategy live-confirmation UX; any
  change to `live_broker.py` / `execution/broker.py` (reused, untouched,
  because the runner never calls them). `LIVE_TRADING_ENABLED` stays `false`.
- Phase 63: paper-trading execution (2026-09-10, D081) — puts a validated
  `StrategyVersion` on a scheduled PAPER-trading runner, behind a mandatory
  human-approval gate. The first order-placement path with no HTTP request
  and no human in the loop for the specific order. Built by 2 agents
  (backend: models + migration `0024` + state machine + runner + routes;
  frontend: page + 8 proxy routes + 4 components).
  **(1) The gate is a column, not a convention** — a deployment is created
  `pending_approval`; the runner's enumeration is `WHERE status = 'active'`;
  the only way to `active` is the explicit `POST /deployments/{id}/approve`,
  which is gated by a **separate, stricter** permission
  `strategy:approve_deployment` (a `strategy:deploy`-only caller gets 403).
  State machine: `pending_approval → active ⇆ paused`, any non-terminal →
  `stopped` (terminal).
  **(2) `STRATEGY_RUNNER_ENABLED` defaults false** — same fail-closed
  posture as the snapshot scheduler, the reconciler, live trading and the
  emergency stop. `mode='paper'` only (Phase 64 adds `live`).
  **(3) The runner is a headless backtest replay loop** — reuses
  `evaluate_current_signal` (Phase 61) so a deployment can never disagree
  with `POST .../signals`, `_desired_quantity` / `_warmup_bar_count`
  (engine_v2, the D072 cross-module precedent), the single risk-engine
  resize retry `backtesting/engine.py::_attempt_trade` also does,
  `submit_trade_and_record` (the one sanctioned path), and
  `load_market_risk_inputs` (Phase 62). `require_stop_price=False` (D035 —
  the exit rule IS the stop). Close-of-bar execution (`now` = the bar's ts).
  **(4) Fail-closed, always audited** — every cycle writes an append-only
  `strategy_deployment_runs` row that always resolves to a terminal status
  (the D072 posture); the global emergency stop halts every cycle
  (`skipped_emergency_stop`); a UTC-weekend gate no-ops it; a third
  advisory-lock objid gives cross-worker exclusion; an unpriceable held
  position FAILS the cycle visibly (no fabricated mark).
  **(5) Migration `0024`** — `strategy_deployments` + `strategy_deployment_runs`
  (version/broker `RESTRICT`); `orders` / `signal_evaluations` each gain a
  nullable `deployment_run_id` `SET NULL` FK. Round-trips clean.
  **1071 backend tests** (1050→1071, +21: 9 runner, 7 service, 5 API);
  `ruff`, `mypy apps` (140 files, up from 135), `secret_scan` clean.
  **292 frontend tests / 36 files** (273/34 → 292/36; +19 across
  `CreateDeploymentForm.test.tsx` and `DeploymentList.test.tsx`),
  `npm run build` clean. No new dependency, no `SideNav` entry (deployments
  live under a strategy, reached by URL).
  Not built (deliberately): auto-unwinding positions on stop, per-order
  human approval beyond the deployment gate, a cross-user approval
  workflow, live mode.
- Phase 62: risk / position-sizing v2 (2026-09-10, D079) — adds the two
  market-risk constraints D029 deferred for want of a price series, now
  that Phase 53's `market_data_bars` supplies one. Built by 2 agents
  against a frozen core (models + manager + `marketdata/portfolio_risk.py`)
  the orchestrator wrote first. **No migration** (`portfolio_binding_constraint`
  is `String(64)`), **no frontend change** (the value renders as a raw
  string).
  **(1) `decide()` stays zero-I/O** — gains one optional 4th param
  `market_risk: MarketRiskInputs | None`; `routes/trades.py` reads the
  trailing year of daily bars for the proposed + held symbols, computes the
  stats (`load_market_risk_inputs`), and hands them in as plain data, right
  where it already reads `recent_orders` and the emergency-stop bool.
  **(2) Every backtest is byte-identical** — `backtesting/engine.py` (the
  one funnel for walk-forward / scan / robustness / engine_v2) never passes
  `market_risk`, so the two checks never appear in a backtest decision and
  no Phase 55–61 result moves.
  **(3) Fail-open, audited** — a symbol with < 60 overlapping daily returns
  in the trailing 365 days makes the check a recorded `PortfolioCheck`
  with `skipped=True, passed=False, worsened_by_trade=False`; it names the
  gap and does not block. The 3 D029 checks still gate. Fabricating a
  covariance is the worse failure (TRADING_SAFETY), and fail-closed would
  block every un-backfilled symbol.
  **(4) `PORTFOLIO_VOLATILITY`** = projected `sqrt(wᵀΣw)` on post-trade
  weights vs `portfolio_max_portfolio_volatility_pct` (0.40); MODIFY sizes
  down by bisection on integer quantity; never blocks a de-risking sell.
  **(5) `POSITION_CORRELATION`** = max Pearson correlation of the proposed
  symbol vs any held OTHER symbol vs `portfolio_max_position_correlation`
  (0.80); quantity-independent, so it only blocks OPENING a new position,
  and it is a REJECT (cap 0), never a MODIFY.
  **(6) Config** — 4 new settings (0.40, 0.80, 365d, 60 obs) + a
  `_enforce_sane_market_risk_settings` validator; annualization constant
  252 (not a setting).
  **1050 backend tests** (1021→1050, +29 test functions: 15
  `test_manager_market_risk`, 10 `test_portfolio_risk`, 4
  `test_trades_portfolio_market_risk`; 1 assertion updated in `test_manager`
  for the grown enum). `ruff`, `mypy apps` (135 files, up from 134),
  `secret_scan` clean. No migration.
  `alembic downgrade -1` → `upgrade head` still round-trips clean. Frontend
  unchanged (273/34). `LIVE_TRADING_ENABLED` stays false — this posture
  would be revisited before live.
  Not built (deliberately): sector concentration / expected-return /
  drawdown constraints (no data), backtest-engine wiring (would move every
  result), fail-closed on thin data, a persisted covariance table, a
  closed-form MODIFY solve.
- Phase 61: signal engine (2026-09-10, D078) — turns a validated strategy
  into "what does it say to do about this symbol right now," off the latest
  ingested bars, with the reasoning kept. Built by 2 agents (backend engine
  + routes + migration `0023`; frontend page + form + table). Where Phase
  55's backtest asks "what would this have done over that window," this
  asks "what does it say at the newest bar."
  **(1) The headline signal IS the backtest's** —
  `evaluate_current_signal` takes `generate_signals(bars, definition)[-1]`,
  the last element of the exact series `backtesting/executor.py` would
  produce over those bars; the per-rule breakdown is
  `strategies/expressions.py::evaluate_rule`. Both unchanged. The one thing
  added is a human-readable `explanation` naming the concrete numbers
  (spec section 25's "never a black-box BUY").
  **(2) Insufficient data is a persisted answer** — a symbol with no bars,
  or too few for its indicators, is a real `SignalEvaluation` row:
  `signal="hold"`, `insufficient_data=True`, an `explanation` naming how
  many bars exist. Never an exception, never a fabricated HOLD.
  **(3) `entry_rule_held` / `exit_rule_held` are three-valued** — `true` /
  `false` / `null` for "could not be evaluated there"; `null` is never
  collapsed to `false`. `insufficient_data` still `false` when the exit
  rule alone settles a SELL (exit beats entry).
  **(4) `strategy:signal` is a NEW permission** — not `strategy:backtest`.
  A signal is a present-tense instruction (the input Phase 63's runner
  acts on); studying a strategy's past must not grant asking what it says
  now. No ADMIN override; ownership re-derived per request.
  **(5) On demand, synchronous in-request** — ≤50 symbols, each one
  indexed read + in-memory evaluation. No recurring job in this phase.
  **(6) The persisted bar store (D070) is the only source** — never a live
  vendor call. `MarketDataStore.get_latest_bars` added (newest-N, returned
  oldest-first, never padded).
  **(7) `indicator_values` / `latest_close` as strings, `null` never 0**;
  `as_of_bar_date` / `latest_close` `null` only in the zero-bar case,
  never back-filled.
  **(8) No summary/detail split** — a signal evaluation has no large child
  collection; the list route returns the full shape (the `explanation` is
  its most useful field). The POST response has no `limit`/`offset`.
  **1021 backend tests** (976→1021, +45: engine unit tests incl.
  parametrized families, 3 store, 7 API), **273 frontend tests across 34
  files** (259/32→273/34, +14). `ruff`, `mypy apps` (134 files, up from
  130), `secret_scan`, `npm run build` all clean. Migration `0023`
  round-trips clean. Verified on a fresh isolated Postgres/Redis.
  Not built (deliberately): any recurring/scheduled evaluation (Phase 63),
  a live vendor fetch when the store is short, a `SideNav` entry (signals
  live under a strategy), a summary DTO.
- Phase 60: universe scanning (2026-09-10, D077) — run one validated
  strategy across many symbols, ranked by performance. Built by 2 parallel
  agents (backend orchestration + migration `0022`; frontend scan form +
  ranked results). Where walk-forward asks "does this hold up across
  periods," this asks "which markets does it suit."
  **(1) `run_strategy_backtest` is CALLED, never re-implemented** — every
  scanned symbol is a real, fully-persisted `BacktestRun`, and
  `universe_scan_results.backtest_run_id` points at it. Right here for the
  same reason walk-forward reuses real runs and Phase 58's robustness
  can't: a scanned symbol replays the version's OWN unaltered definition.
  **(2) Every symbol starts fresh at the same `starting_cash`** — a
  comparison, not a path-dependent shared-portfolio simulation (which is a
  different feature).
  **(3) `MAX_SCAN_SYMBOLS = 50`** — synchronous in-request, one backtest
  per symbol; the cap is honest about what that can do until a job runner
  exists. The route 422s (before any row) for an over-cap list, an
  explicit `[]`, or "all ingested" mode with 0 or >50 distinct ingested
  symbols — each naming the real count.
  **(4) Two modes, one resolver** (`resolve_scan_symbols`, called twice
  per request): `symbols` omitted → scan every distinct symbol in
  `market_data_bars` for the interval; a list → normalized explicit scan.
  `requested_symbols` is the first `ARRAY` column since `Role.permissions`.
  **(5) A per-symbol backtest failure is a normal recorded outcome** (a
  symbol with no ingested bars) — its own result row with the real FAILED
  `backtest_run_id`, metrics NULL, scan continues. A `SUCCEEDED` scan with
  `num_succeeded == 0` is a real result, not an error.
  **(6) Ranking computed on read** (no `rank` column), `num_qualified` =
  "succeeded and profitable" — a stated, adjustable first-pass filter.
  **(7) Reuses `strategy:backtest`** — a scan is a batch of backtests.
  **976 backend tests** (954→976, +22), **259 frontend tests across 32
  files** (240/29→259/32, +19). `ruff`, `mypy apps` (130 files, up from
  127), `secret_scan`, `npm run build` all clean. Migration `0022`
  round-trips clean. Verified on a fresh isolated Postgres/Redis; shared
  dev stack's familiar stale-broker-row flake resurfaced during
  verification and does not reproduce on a clean DB. (Frontend suite is
  flaky at default worker concurrency on this slow test machine — use
  `--maxWorkers=2 --testTimeout=20000`; environmental, not a regression.)
  Not built (deliberately): a shared-portfolio universe backtest (a
  different feature), a persisted `rank` column, an uncapped/asynchronous
  scan (needs a job runner this codebase lacks), a `SideNav` entry
  (universe scans live under a strategy, reached from its detail page).
- Phase 59: strategy ranking / leaderboard (2026-09-10, D076) — turns the
  raw numbers Phases 55/57/58 persist into a ranked leaderboard of the
  caller's own strategies. Built by 2 parallel agents (backend scoring +
  route; frontend page).
  **(1) A transparent read-model — no `strategy_scores` table, no
  migration.** The score is recomputed on every request from existing
  `backtest_runs`/`walk_forward_runs`/`monte_carlo_runs`/`robustness_runs`
  rows (four indexed lookups per strategy version). First route in this
  whole initiative with no schema change.
  **(2) Never a black box.** Four equally-weighted 25-point components
  (return, risk, out-of-sample consistency, parameter stability); return
  capped at a quarter of the total so a strategy can't rank on ROI alone.
  Every component carries a `detail` string naming the actual column value
  and the scale; every score carries a `status_reason` naming the counts
  behind it; four `latest_*_run_id` fields make it auditable.
  **(3) Absent is never zero.** A component with no underlying run is
  omitted entirely (never scored 0); `percentage` (not raw points) is the
  ranking key, and `components_measured` (0-4) is always surfaced. A
  strategy with no succeeded backtest has no score and is excluded from
  the leaderboard.
  **(4) A documented, adjustable status heuristic**: `insufficient_data` /
  `promising` / `validated` / `overfit_risk` (one warning sign —
  walk-forward profitable ratio < 0.5 or robustness deviation > 20pp —
  flags it, taking priority over `validated`). An `overfit_risk` strategy
  satisfies no `min_status` filter above the floor.
  **(5) An empty leaderboard is a 200, never an error** — the master
  spec's "no sufficiently robust strategy found" is exactly that answer.
  **(6) Caught a real cross-phase bug**: D074's walk-forward orchestrator
  persists a real `backtest_runs` row per window, so "latest backtest by
  created_at" was picking up a walk-forward window fragment instead of the
  headline backtest for any version ever walk-forward tested. The backend
  agent's integration test (real four-phase route chain over real bars)
  caught it; fixed by excluding window rows, pinned by a regression test.
  **(7) Reuses `strategy:manage`** (a read over an already-gated
  resource). `/strategies/leaderboard` registered before
  `/strategies/{strategy_id}` in `main.py` — the one place registration
  order is load-bearing (a `uuid.UUID` path param would otherwise 422 the
  static path), commented as such.
  **954 backend tests** (916→954, +38: 32 unit in
  `tests/strategies/test_scoring.py`, 6 integration in
  `tests/api/test_leaderboard.py`), **240 frontend tests across 29 files**
  (232/28→240/29, +8). `ruff`, `mypy apps` (127 files, up from 124),
  `secret_scan`, `npm run build` all clean. Verified on a fresh isolated
  Postgres/Redis; shared dev stack's familiar stale-broker-row flake
  resurfaced during verification and does not reproduce on a clean DB.
  Not built (deliberately): a persisted score (see (1)), a weighted
  composite (equal weighting is a stated choice), a Monte Carlo points
  component (`latest_monte_carlo_run_id` is surfaced for reference only —
  sequence risk isn't a pass/fail signal), an ADMIN cross-user
  leaderboard.
- Phase 58: parameter-sensitivity (robustness) testing (2026-09-09, D075)
  — closes the scope split out of the original "walk-forward, robustness,
  Monte Carlo" roadmap line (D074). The platform's first concrete answer
  to the anti-overfitting requirement: replay a validated
  `StrategyVersion` once as authored, then once per numeric parameter
  nudged ±`magnitude_pct` (one factor at a time, every other parameter
  held fixed), and report how far the result moved.
  **Built by two subagents in parallel; one was interrupted mid-task by a
  session rate limit** while writing its own integration tests. Its work
  through that point (migration `0021`, both ORM models, the orchestrator,
  schemas, routes, `main.py` registration, complete test files) was
  inspected rather than redone, and needed exactly one fix: one
  integration test's fixture used an invalid `"open"` rule operand (this
  platform's closed vocabulary only recognizes `"close"`, per D071) —
  fixed in the test, zero production code touched.
  **(1) `apps/api/app/strategies/perturbation.py`** — pure, no I/O,
  generates two variants per perturbable parameter (indicator periods,
  and `position_sizing.fraction`/`amount` when applicable), one at a time,
  clamped into each parameter's own valid range; a direction whose clamp
  produces no real change is skipped. A definition with nothing numeric
  to perturb (`all_in`, no indicators) returns an empty list — a real,
  valid outcome, not an error.
  **(2) `apps/api/app/backtesting/robustness.py`** — replays the baseline
  and every perturbation through `engine_v2.py`'s own already-tested,
  no-persistence helpers (`_load_warmup_and_window`/`_replay_window`,
  reused across the module boundary, the same precedent D072 set for
  `_attempt_trade`); the warmup is recomputed PER perturbed definition,
  never shared from the baseline, since a nudged period needs a different
  warmup count. **A perturbation is never persisted as a `BacktestRun`**
  — it replays a definition no `StrategyVersion` actually contains, so it
  gets its own `robustness_perturbations` row (migration `0021`) instead.
  **(3) `max_return_deviation_pct` is one plain, honest number, not a
  composite "robustness score"** — no weighted figure across return/
  drawdown/etc. is defined, since any weighting would encode an unstated
  risk preference (echoing D071's identical refusal to invent a strategy-
  quality score).
  **(4) Reuses the existing `strategy:backtest` permission** — another
  analysis of a backtest, not a new capability class, same reasoning
  walk-forward and Monte Carlo already established.
  **916 backend tests** (878→916, +38: 22 in `tests/strategies/
  test_perturbation.py`, 16 in `tests/backtesting/test_robustness.py` +
  `tests/api/test_robustness.py`); none deleted, skipped, or weakened
  (one test's own fixture was corrected, not weakened). `ruff check` and
  `mypy apps` (124 source files, up from 120) clean; `bash
  scripts/secret_scan.sh` clean. Verified on a freshly-migrated, isolated
  Postgres/Redis via a full `alembic upgrade` → `downgrade -1` →
  `upgrade head` round-trip and the complete suite; the shared dev
  stack's familiar stale-broker-row flake resurfaced during verification
  and, again, does not reproduce on a clean database.
  Not built (deliberately): regime-slice reporting (bull/bear/sideways
  performance breakdown — explicit further scope within this phase per
  the roadmap, not attempted here), any frontend surface (backend only,
  same posture as Phases 53/55/57), a composite robustness score (see (3)
  above), combined/simultaneous multi-parameter perturbation (see D075's
  Alternatives Rejected).
- Phase 57: walk-forward validation + Monte Carlo simulation (2026-09-09,
  D074) — both build directly on Phase 55's persisted `BacktestRun`.
  **Mid-phase scope note**: the original roadmap bundled "walk-forward,
  robustness, Monte Carlo" together; robustness (parameter-perturbation)
  testing is genuinely distinct work and was split out to its own **Phase
  58**, shifting everything after it by one — the saved plan file was
  updated to match. Built by two subagents in parallel; the orchestrator
  built the shared DB migrations/models centrally first, since both
  features needed one in the same phase.
  **(1) Walk-forward** (`walk_forward_runs`/`walk_forward_windows`,
  migration `0019`) — sequential, non-overlapping windows over a
  requested range, each a REAL `BacktestRun` (`engine_v2.run_strategy_backtest()`
  reused unchanged, once per window, never a second execution path).
  Deliberately framed as consistency testing, not "walk-forward
  optimization" — this platform has no parameter-fitting step to walk
  forward. Aggregates (mean/stddev/best/worst return, profitable-window
  count) are computed only over windows whose own backtest succeeded; a
  range with fewer than 2 complete windows, or where every window failed,
  is a real `FAILED` run naming why.
  **(2) Monte Carlo** (`monte_carlo_runs`, migration `0020`) — seeded
  bootstrap resampling (with replacement) of an existing successful
  `BacktestRun`'s trade returns into percentile bands (5th/50th/95th) on
  final equity and max drawdown, plus a probability-of-ruin figure. Only
  aggregate statistics are persisted, never every simulated path — the
  persisted `random_seed` makes the exact distribution reproducible on
  demand instead. **Reproducibility is proven by test, not assumed**: two
  runs with an identical explicit seed produce bit-for-bit identical
  results across all eight aggregate columns.
  **(3) Both reuse the existing `strategy:backtest` permission** (Phase
  55) rather than adding a new one each — both are further analyses of a
  backtest, not a new capability class.
  **878 backend tests** (840→878, +38: 19 in `tests/backtesting/
  test_walk_forward.py` + `tests/api/test_walk_forward.py`, 19 in
  `tests/backtesting/test_monte_carlo.py` + `tests/api/test_monte_carlo.py`);
  none deleted, skipped, or weakened. `ruff check` and `mypy apps` (120
  source files, up from 114) clean; `bash scripts/secret_scan.sh` clean.
  Verified on a freshly-migrated, isolated Postgres/Redis via a full
  `alembic upgrade` → `downgrade -2` → `upgrade head` round-trip and the
  complete suite; the shared dev stack's familiar stale-broker-row flake
  (D070/D071/D072) resurfaced during development and, again, does not
  reproduce on a clean database.
  Not built (deliberately): parameter-perturbation robustness testing and
  regime-slice reporting (now Phase 58), any frontend surface for either
  feature (a future phase's concern — this phase is backend-only, same
  posture as Phase 53/55), a job queue for either (both run synchronously
  in-request, matching every other analysis job in this codebase).
- Phase 56: backtest analytics dashboard (2026-09-08, D073) — the first
  frontend surface for anything Phase 54/55 built, and the first real
  charting library in this codebase (Recharts `^3.10.1`, scoped only to
  the 5 new Strategy Lab chart components; every existing chart stays
  hand-rolled SVG, untouched). Built by two subagents in parallel, fully
  decoupled by design.
  **(1) `/strategies/[id]/backtests`** — version selector (all versions
  listed, non-validated ones disabled with a hint), a `RunBacktestForm`,
  a `BacktestRunList` with multi-select, and a `BacktestRunComparison`
  (equity overlay + metrics table) for 2+ selected runs.
  **(2) `/strategies/[id]/backtests/[runId]`** — equity curve, drawdown
  curve, monthly-returns heatmap, and a trade ledger, all computed
  client-side from the run's already-returned `equity_curve`
  (`lib/backtestMetrics.ts`, pure and unit-tested) rather than persisted
  server-side.
  **(3) A `status: "failed"` run is rendered honestly everywhere**: the
  detail page shows the real error and skips charts/metrics entirely, the
  list shows `—` not `0%`, the comparison view excludes it from the
  overlay chart but keeps it (with its error) in the metrics table.
  **(4) A real Recharts+jsdom pitfall was found and documented** (D073):
  a `<LineChart>` with both a `<Legend>` and 2+ series silently drops
  `<Line>`/`<YAxis>`/`<CartesianGrid>` under this app's test environment;
  worked around independently (a scoped `getBoundingClientRect` mock in
  one test file, a hand-rolled legend in production code in the other) —
  worth checking first if a future multi-series chart's tests mysteriously
  fail.
  **232 frontend tests across 28 files** (183/20→232/28, +49 this phase);
  none deleted, skipped, or weakened. `npm run build` clean, every new
  route and both new proxy routes present in the manifest. Exactly one new
  dependency added (confirmed via `git diff package.json`).
  Not built (deliberately): reusing `EquityCurveChart` inside
  `BacktestRunComparison` (a small, deliberate duplication traded for full
  agent-parallelism safety — see D073), any backend change (this phase
  consumes Phase 55's API as-is).
- Phase 55: a pluggable, persisted backtesting engine (2026-09-08, D072) —
  the first thing that actually RUNS a `StrategyDefinition`. Built by two
  subagents in parallel against one frozen interface
  (`executor.generate_signals(bars, definition) -> list[Signal]`); the
  persistence-side agent's route-level tests ran against the real
  evaluator, not a stub, with no reconciliation needed.
  **(1) `apps/api/app/strategies/expressions.py`** — reusable rule-
  evaluation primitives over a `StrategyDefinition` + `list[Bar]`,
  deliberately not backtest-specific (Phase 60's live signal engine will
  reuse it). All six D071 operators implemented; `crosses_above`/
  `crosses_below` proven by test to NOT be sugar for the instantaneous
  comparisons. No code execution anywhere — same hard rule as
  `validation.py`.
  **(2) `apps/api/app/backtesting/executor.py`** — `generate_signals()`,
  **proven elementwise-identical** to D025's hard-coded `strategy.py` on
  the logically-equivalent SMA(20)-crossover definition, across 7 warmup-
  period values. Exit-fires-before-entry precedence when both conditions
  hold on the same bar.
  **(3) `BacktestRun`/`BacktestEquityPoint`/`BacktestTrade`** (migration
  `0018`) — `strategy_version_id` is `ON DELETE RESTRICT`. A run is
  created `RUNNING` and always resolves to a terminal status; unlike D025's
  v1, **a failure is returned as a `FAILED` row, never raised as an
  exception that persists nothing** — v2 always has a row to finish.
  **(4) Real position sizing** (`all_in`/`fixed_fraction`/
  `fixed_notional`) — the one thing v1 never had (it hard-codes "all
  available cash"). `fixed_fraction` sizes off equity, not cash,
  deliberately.
  **(5) The RISK→PORTFOLIO→BROKER sequence is imported from `engine.py`**
  (`_attempt_trade`, across the module boundary, commented) rather than
  duplicated — `engine.py`/`strategy.py`/`metrics.py`/`errors.py`/
  `models.py` are all untouched and stay that way; `POST /backtests`
  (D025) still runs unchanged, side by side with the new
  `/strategies/{id}/versions/{id}/backtests` + `/backtest-runs/{id}`
  routes.
  **(6) New permission `strategy:backtest`**, separate from
  `strategy:manage`, gating the new routers; per-row ownership checked on
  top via the run's `strategy_version → strategy → owner_user_id` chain.
  409 (`VERSION_NOT_VALIDATED`) backtesting anything but a validated
  version.
  **840 backend tests** (778→840, +62: 44 pure-unit in
  `tests/strategies/test_expressions.py` + `tests/backtesting/
  test_executor.py`, 18 DB-backed in `tests/backtesting/test_engine_v2.py`
  + `tests/api/test_strategy_backtests.py`); none deleted, skipped, or
  weakened. `ruff check` and `mypy apps` (114 source files, up from 109)
  clean; `bash scripts/secret_scan.sh` clean. Verified on a freshly-
  migrated, isolated Postgres/Redis via a full `alembic upgrade` →
  `downgrade -1` → `upgrade head` round-trip and the complete suite,
  confirming the one failure seen during development (the same stale
  shared-dev-DB row D070/D071 already documented) does not reproduce on a
  clean database.
  Not built (deliberately): any frontend surface (the analytics dashboard
  is Phase 56, which is also where Recharts is introduced), short/sell-
  side trades (every completed trade row is `side="buy"` — a documented
  scope limit, not a bug: the replay only ever opens long, mirroring
  D025), and intraday bar intervals (still only `"1d"`, per Phase 53's own
  scope).
- Phase 54: user-owned strategies with immutable-once-validated versions
  (2026-09-08, D071) — the Strategy Lab's first real domain object. Built
  by two subagents in parallel (backend + frontend) against one frozen
  API contract, reconciled and re-verified centrally.
  **(1) `Strategy`/`StrategyVersion`** (migration `0017`) — a strategy
  belongs to exactly one owner; a version is immutable once its status
  leaves `draft`, enforced as a 409 at the route layer. `definition` is
  the first JSONB column in this schema (deliberate — see D071).
  **(2) `apps/api/app/strategies/`** — a closed-vocabulary
  `StrategyDefinition` (indicators: `sma`/`rsi`; rule operators:
  `crosses_above`/`crosses_below`/`gt`/`gte`/`lt`/`lte`; position sizing:
  `all_in`/`fixed_fraction`/`fixed_notional`) and a purely structural
  `validate_definition()` that never executes anything — no `eval`, no
  `exec`, no `compile` — and reports every problem it finds, not just the
  first.
  **(3) New permission `strategy:manage`**, gating a new 8-route
  `/strategies` router; ownership (`owner_user_id`) is checked
  per-request on top of the permission, so holding the permission only
  ever grants access to one's own strategies.
  **(4) Frontend**: `/strategies` (list + create) and
  `/strategies/[id]` (detail, version history, fork, a form-based
  `StrategyBuilderForm` — indicator rows, rule operator/operand selects,
  conditional sizing fields; not a raw JSON textarea, not a visual
  node/flow editor). A non-draft version is read-only client-side too,
  not just server-side. One new `SideNav` entry ("Strategy Lab"). No new
  dependency.
  **778 backend tests / 183 frontend tests across 20 files** (705→778
  backend since D070, +52 this phase; 170/18→183/20 frontend, +13 this
  phase); none deleted, skipped, or weakened. `ruff check` and `mypy apps`
  (109 source files, up from 103) clean; `bash scripts/secret_scan.sh`
  clean; `npm run build` clean with every new route present in the
  manifest. Verified on a freshly-migrated, isolated Postgres/Redis
  (55432/56379) via a full `alembic upgrade` → `downgrade -1` →
  `upgrade head` round-trip and the complete backend+frontend suites,
  confirming the one failure seen during development (the same stale
  shared-dev-DB `AAPL.US` position D070 already documented) does not
  reproduce on a clean database and is not caused by this phase.
  Not built (deliberately): anything that reads a `StrategyDefinition` to
  actually run it (backtesting is Phase 55 — nothing here touches
  `apps/api/app/backtesting/`, `marketdata/`, `risk/`,
  `portfolio_manager/`, or `execution/`), an `ADMIN` override for
  managing another user's strategies, a visual node/flow builder, and a
  Playwright e2e spec — the last one matches Phase 50's watchlists (the
  closest precedent: a full user-owned CRUD resource with a real UI),
  which also shipped without one.
- Phase 53: persisted historical OHLCV bar store + manual, on-demand
  ingestion (2026-09-08, D070) — the prerequisite the new Strategy Lab
  initiative (a user-supplied 70-section master prompt, phased as this
  project's Phase 53 onward per the approved plan) sits on. **No
  strategy/backtest code exists yet and none is touched.** Backend only;
  `apps/web` untouched by design (this phase has no UI surface).
  **The gap**: no persistent historical OHLCV bar storage existed anywhere
  in this codebase, even though the Postgres image has been
  `timescale/timescaledb` since Phase 1 — `HistoryProvider` (D021) only
  ever exposes "the most recent N daily closes as of now," fetched live on
  every call, closes-only, no date-range parameter, which is exactly why
  `backtesting/engine.py` (D025) is hard-limited to `end_date == today`.
  Three parallel codebase explorations confirmed this is the single
  biggest blocker to everything else the master prompt asks for.
  **(1) `market_data_bars` + `market_data_backfill_jobs`** (migration
  `0016`) — the first hypertable in this codebase
  (`CREATE EXTENSION IF NOT EXISTS timescaledb;` runs for the first time
  here, verified for real against `timescaledb_information.hypertables`).
  Natural composite `(symbol, bar_interval, ts)` primary key, not this
  schema's usual random-UUID `_uuid_pk()` — a hypertable's constraints
  must include the partitioning column, and the natural key makes
  idempotent re-ingestion trivial. Only `bar_interval='1d'` is populated;
  the column exists for future intraday intervals.
  **(2) `HistoricalBarProvider`** (`apps/api/app/marketdata/bar_provider.py`)
  + **`MarketDataStore`** (`apps/api/app/marketdata/store.py`) — a new,
  separate read/write port for the persisted bar store, deliberately not a
  replacement for `HistoryProvider` (the live analyst layer keeps using
  that for its own, different, most-recent-N-closes need).
  **(3) `LongbridgeBarBackfillProvider`**
  (`apps/api/app/marketdata/providers/longbridge.py`) — wraps the vendor
  SDK's `history_candlesticks_by_date()` (verified by direct introspection
  of the installed `longport` v4.3.7 package), real OHLCV over an
  arbitrary date range, same all-or-nothing credential gate as every other
  Longbridge provider.
  **(4) `apps/api/app/marketdata/ingestion/backfill.py`** — orchestrates
  one on-demand job (create → fetch → upsert → record outcome, one
  transaction); a vendor failure is recorded `FAILED` with the real error,
  never silently "zero bars, success." No scheduled/automatic backfill —
  manual only, same posture the backtest engine takes toward its own
  execution.
  **(5) `POST /admin/market-data/backfill`** — gated by the existing
  `Permission.ADMIN` (no new permission this phase). Runs synchronously
  to completion in the request (no job queue exists). Returns 201 whether
  the job succeeded or failed — a completed attempt is a real, auditable
  resource either way, not an HTTP-level error.
  **726 tests passing** (705 pre-existing baseline + 21 new: 6 vendor-unit
  in `tests/marketdata/providers/test_longbridge_bar_backfill.py`, 4 in
  `tests/marketdata/test_store.py` covering upsert idempotency, 4 in
  `tests/marketdata/test_backfill.py` covering job success/failure/
  overlapping re-ingestion, 7 route-level in
  `tests/api/test_admin_market_data.py`); **none deleted, skipped, or
  weakened**. `ruff check` and `mypy apps` (103 source files, up from 99)
  both clean; `bash scripts/secret_scan.sh` clean. Verified against a
  freshly-migrated, isolated Postgres/Redis on remapped ports (55432/56379
  — never the shared dev stack's 5432/6379), including a real
  `alembic upgrade head` → `downgrade -1` → `upgrade head` round-trip and a
  live `SELECT * FROM timescaledb_information.hypertables` confirming
  `market_data_bars` is a genuine hypertable. A real vendor smoke test was
  attempted and honestly reported: this checkout's `.env` only documents
  the Longbridge credential trio in comments without setting real values,
  so `build_longbridge_bar_backfill_provider()` correctly returned `None`
  and the live-network half of verification could not run here — the
  NOT_CONFIGURED path itself is exactly what one of the new tests asserts.
  Also discovered in passing (not fixed, not in scope): the shared dev
  Postgres (port 5432, the `trading_os_pgdata` Docker volume) holds a
  stale broker row with a leftover "AAPL.US" position from unrelated
  earlier debugging, which makes the *pre-existing*
  `test_snapshot_scheduler_market_hours.py` weekday-control test flake
  there; re-running the full suite on a clean isolated stack confirmed
  this phase's changes are not the cause.
  Not built (deliberately): any strategy/backtest code reading from this
  store (Phase 54+), any automatic/scheduled recurring backfill, intraday
  intervals, or any frontend surface.
- Phase 49: reconciliation of accepted-but-unexecuted LIVE orders — the
  gap Phase 43/D058 recorded as not built (2026-09-03, D066). **No real
  order was ever placed, no real broker was ever contacted, and
  `LIVE_TRADING_ENABLED` was never set to `true` — not in a default, not in
  a fixture, not transiently.** Backend only; `apps/web` untouched, and
  `apps/api/app/api/routes/trades.py` was NOT modified (a sibling phase
  owned that file).
  **(0) The gap was worse than documented, and reading the code found it.**
  D058 and docs/TRADING_SAFETY.md both described the missing piece as "no
  reconciler". In fact **nothing was persisted at all**:
  `LiveOrderNotFilledError` is raised inside `submit_trade()`, *before*
  `submit_trade_and_record()` builds its `OrderRow`, and the route's
  `502 LIVE_ORDER_UNCONFIRMED` means `session.commit()` is never reached —
  so the request rolled back and the system forgot it had placed a real
  order. `OrderStatus` also had only `FILLED`/`REJECTED`, neither of which
  is true of an unknown outcome. The producing half therefore had to be
  built first; both halves are this phase.
  **(1) `SUBMITTED_UNCONFIRMED` and `BROKER_CLOSED_UNFILLED`** — two new
  `OrderStatus` values plus `orders.broker_order_id` (UNIQUE),
  `orders.broker_status`, `orders.reconciled_at`, in migration `0014`
  (verified upgrade + downgrade + upgrade round-trip against real
  Postgres). `BROKER_CLOSED_UNFILLED` is deliberately distinct from
  `REJECTED`, which means *this system* blocked the trade and it never
  reached a venue. The venue's own status string is preserved verbatim in
  `broker_status` rather than fanned out into three near-identical enum
  values.
  **(2) The unconfirmed order is recorded and committed** —
  `_record_unconfirmed_order()` in `apps/api/app/oms/persistence.py`, the
  one documented exception to that module's no-commit rule. Safe
  structurally, not incidentally: the live branch of `_execute_trade()`
  never calls `load_paper_broker()`, so no `FOR UPDATE` lock is held and no
  paper write is pending. The recorded quantity is the one **actually sent**
  (post-Portfolio-Manager), carried out on a new port-level
  `OrderNotConfirmedError` that `LiveOrderNotFilledError` now subclasses —
  so the pure OMS needs no import of a concrete live adapter and every
  existing `except` arm, including both in `trades.py`, catches exactly
  what it did before. If that context is ever missing, **no row is written**
  rather than one guessing at the quantity.
  **(3) `LiveOrderReconciler`** (`apps/api/app/execution/reconciliation.py`)
  — an opt-in interval loop, a **sibling** of `PortfolioSnapshotScheduler`
  (started/stopped in the same lifespan, not replacing it), on D047's exact
  advisory-lock mechanism with its own object key (`b"recn"`) so the two
  jobs never exclude each other. `LIVE_ORDER_RECONCILER_ENABLED` defaults
  false, and even enabled every cycle short-circuits **before** the lock and
  before any SQL unless a real live adapter exists — so on the committed
  configuration it costs one log line per interval and nothing else.
  `LiveBrokerAdapter.get_order_status()` is a new real method over the same
  `order_detail()` SDK call, added rather than a second client.
  **(4) Nothing is inferred from the passage of time.** An order the broker
  still calls working is left completely untouched — not aged out, not
  timed out, `reconciled_at` not even stamped. `_TERMINAL_STATUSES` was read
  off the installed SDK by direct introspection (which caught that it
  spells cancellation `Canceled`); every unlisted status, `PartialFilled`
  and `Unknown` included, counts as still open. A failing broker call skips
  that one order and changes nothing. Fills come only from the broker's own
  `executed_quantity`/`executed_price`, never the order's
  `estimated_price`. The single permitted UPDATE carries
  `WHERE status = 'submitted_unconfirmed'` and is decided by `rowcount`, so
  a terminal row can never be rewritten and a fill can never be
  double-written — enforced in Postgres, not by this code being careful.
  **(5) Observability** — `/health` gains
  `live_order_reconciler: "DISABLED"|"enabled:<n>s"` (still zero-I/O), and
  the startup log gains it plus `live_order_reconciler_cycle_lock`.
  **657 tests passing** (615 pre-existing baseline re-measured on this
  branch before any edit, + 42 new: 25 unit in
  `tests/execution/test_reconciliation.py`, 17 DB-backed integration in
  `tests/api/test_live_order_reconciler.py`); **none deleted, skipped or
  weakened**. `ruff check .` and `mypy apps` clean across 94 source files;
  `bash scripts/secret_scan.sh` clean. Verified against an isolated
  throwaway stack on remapped ports (Postgres 55449, Redis 56449 — never
  the dev stack's 5432/6379/8000/3005), including a real reconciliation
  cycle moving an order from `submitted_unconfirmed` to `filled` with a real
  `fills` row written from a fake live-broker double's figures.
  Not built (deliberately): a reconciliation/order-history endpoint (D062
  already flags that as its own coherent phase — and Phase 48/D065 has since
  landed one on `main`), any frontend, and partial-fill progression
  (`fills.order_id` is UNIQUE; a partially-filled order simply stays under
  observation until the venue calls it done).
- Phase 1: repo skeleton, typed `Settings` with fail-closed live-mode gate,
  secret-redacting structlog, SQLAlchemy 2.0 async models
  (`users`/`roles`/`assets`/`brokers`), migration `0001_initial`
  (verified: upgrade/downgrade/upgrade round-trip against real
  Postgres/TimescaleDB), `GET /health`, CI (ruff/mypy/pytest/migration
  check/secret scan), docker-compose. 5/5 tests passing, ruff+mypy clean.
- Bootstrap: documentation set + root `CLAUDE.md` (2026-08-23).
- Phase 2: deterministic risk engine (2026-08-23). `apps/api/app/risk/`
  (`models.py`, `engine.py`) — pure function, zero LLM/network/I-O
  dependency by construction. Rules enforced: emergency stop, market-data
  freshness, required stop price, non-zero stop distance, max
  single-position size (% of equity), max portfolio exposure (% of
  equity), max per-trade risk (% of equity ÷ stop distance), buying-power
  check. Every rejection returns a typed `BlockReason`, never a bare
  `False`. 14 tests including one that operationalizes the discovery
  report's MVP acceptance criterion (blocks a bad trade with an LLM stub
  that raises). 19/19 total tests passing, ruff+mypy clean.
- Phase 3: paper broker adapter + OMS (2026-08-23).
  `apps/api/app/execution/{broker,paper_broker}.py` — `BrokerAdapter`
  Protocol + deterministic in-memory `PaperBrokerAdapter` (market orders
  only, no shorting, requires a mark for every open position, never
  fabricates a fill price). `apps/api/app/oms/service.py` —
  `submit_trade()`, the single sanctioned path from a proposal to a broker
  call; it always calls the risk engine first and a rejected proposal
  never reaches the broker (proven with a spy adapter in tests). Also
  added `apps/api/app/core/execution_context.py` — typed
  `ResearchContext | PaperContext | LiveContext` so a research code path
  can't structurally hold a live credential. 13 new tests, 32/32 total
  passing, ruff+mypy clean (19 source files).
- Phase 4: order/fill persistence (2026-08-23). Migration `0002` adds
  append-only `orders`/`fills` tables (NUMERIC money columns, denormalized
  `symbol` — see D006). `apps/api/app/oms/persistence.py` —
  `submit_trade_and_record()` wraps (doesn't modify) `submit_trade()` and
  writes exactly one new `Order` row per call, never an update. Verified
  end-to-end against a real Postgres/TimescaleDB container: migration
  upgrade/downgrade/upgrade round-trip, then 3 integration tests exercising
  the actual insert/query path. Found and fixed two real, previously-latent
  bugs during this verification — see D007: (1) `Enum()` columns were
  sending the Python enum member name instead of its value, silently broken
  since Phase 1 but never triggered until a row was actually inserted; (2)
  the async test suite had a cross-event-loop connection bug on Windows,
  fixed by pinning pytest-asyncio to a session-scoped loop. 8 new tests,
  35/35 total passing, ruff+mypy clean (20 source files).
- Phase 5: market data service (2026-08-23).
  `apps/api/app/marketdata/{models,provider,router}.py` —
  `MarketDataProvider` Protocol, `MarketDataRouter` (ordered fallback,
  typed `DataUnavailableError`/`VendorError` vs. any other exception,
  which is never caught), `MarketSnapshot` (normalized quote type).
  **No concrete vendor is wired** — this is deliberately a routing/
  no-fabrication skeleton only; see D008 for why and what's still open.
  9 new tests (all against in-process fakes, no network calls), 44/44
  total passing (41 non-DB + 3 DB-backed, run separately since they need
  a live Postgres), ruff+mypy clean (24 source files).
- Phase 6: HTTP endpoint for trade submission (2026-08-23).
  `POST /brokers/{broker_id}/trades` (`apps/api/app/api/routes/trades.py`)
  — the first externally-reachable path to `submit_trade_and_record()`.
  Looks up the broker (404 if missing, 400 `NOT_CONFIGURED` if not a paper
  broker — no live execution path exists), resolves a per-broker
  `PaperBrokerAdapter` from a new in-process `PaperBrokerRegistry`, builds
  `RiskLimits`/emergency-stop from new `Settings` fields, and returns the
  risk decision plus fill (if any). A missing mark for an existing
  position is a 400 `DATA_UNAVAILABLE`, never a guessed price. Verified
  live against a real running server + Postgres (manual curl, not just
  tests): a fill and a rejection both persisted correctly, readable
  directly via SQL. Found and fixed a second cross-event-loop issue (see
  D009) — FastAPI's `TestClient` runs in its own thread/loop, incompatible
  with the shared DB engine the same way D007's bug was; fixed by using
  `httpx.AsyncClient` + `ASGITransport` instead, which was the correct
  test client for an async app all along. 6 new integration tests, 50/50
  total passing, ruff+mypy clean (30 source files).
- Phase 7: authentication (2026-08-23). `apps/api/app/auth/` — bcrypt
  password hashing, JWT access tokens (`PyJWT`), `POST /auth/login`
  (OAuth2 password flow), and `get_current_user` — now required by
  `POST /brokers/{broker_id}/trades`. `Settings.jwt_secret_key` has no
  default; the app refuses to start without one configured (see D010).
  Migration `0003` adds `orders.submitted_by_user_id`, set on every
  submission. No registration endpoint exists (users are created by
  direct DB insert — deliberate, see D010) and no authorization/role
  checks exist yet (only *authenticate*, not *authorize*). Verified live
  against a running server: unauthenticated request → 401, authenticated
  request with a real token from `/auth/login` → 200 filled, with
  `submitted_by_user_id` confirmed via SQL to match the authenticated
  user. 12 new tests, 67/67 total passing, ruff+mypy clean (36 source
  files).
- Phase 8: role-based authorization (2026-08-23). Migration `0004` adds
  `roles.permissions` (a flat list of permission strings — data-only to
  extend, no migration needed to grant an existing permission to a role).
  `apps/api/app/auth/permissions.py`'s `Permission` enum defines the known
  values (`SUBMIT_PAPER_TRADE`; `SUBMIT_LIVE_TRADE` defined but enforced
  nowhere — no live path exists to gate). New
  `require_permission(Permission)` dependency returns 403 (distinct from
  401) for an authenticated user whose role doesn't grant the permission;
  `POST /brokers/{broker_id}/trades` now requires
  `Permission.SUBMIT_PAPER_TRADE`. `get_current_user` now eager-loads
  `User.role`. 5 new tests (3 pure unit tests against the checker logic,
  2 integration), 72/72 total passing, ruff+mypy clean (37 source files).
  Verified live: a no-role user got 403 with the missing-permission
  detail; a role-holding user got 200 filled — both users created by
  direct SQL insert, confirmed via curl.
- Phase 9: per-broker access grants (2026-08-23). Migration `0005` adds
  `broker_grants` (a `user_id`/`broker_id` join table with a unique
  constraint). New `require_broker_access(Permission)`
  (`apps/api/app/api/dependencies.py`) composes on top of
  `require_permission`: identity → global permission → broker existence
  (404) → specific grant (403). Returns an `AuthorizedBroker(user, broker)`
  so `trades.py` no longer does its own broker lookup. Closes the gap
  D011 explicitly called out — a user with `trade:submit:paper` can no
  longer trade on every `broker_id` they know, only ones they've been
  granted. 2 new tests, 74/74 total passing, ruff+mypy clean (37 source
  files). Verified live against a running server with two real broker
  rows: the same trader got 200 on a granted broker and 403 on an
  ungranted one.
- Phase 10: minimal admin API (2026-08-23). `apps/api/app/api/routes/admin.py`
  — `POST /admin/users`, `POST /admin/roles`, `POST /admin/broker-grants`,
  `DELETE /admin/broker-grants/{id}`, all gated by one new coarse
  `Permission.ADMIN` ("admin:manage"). Closes the D010/D011/D012
  "raw SQL only" gap for day-to-day operation — the very first admin
  user/role still needs one direct DB insert (documented bootstrap step,
  D013), but everything after that goes through the API. Deliberately no
  update/deactivate for users or roles, no listing endpoints — scoped to
  what's actually needed routinely (grants especially, since revoking
  broker access is the realistic day-to-day lever). 9 new tests, 83/83
  total passing, ruff+mypy clean (39 source files). Verified live against
  a running server: bootstrapped one admin via SQL, then created a role,
  a user, and a broker grant entirely through the API, confirming the new
  user could trade only after the grant existed — the same
  grant/trade/revoke/trade-again round-trip is also covered as an
  integration test, not just a manual check.
- Phase 11: persisted paper-broker state (2026-08-23). Migration `0006`
  adds `broker_accounts` (cash) and `broker_positions` (nonzero
  quantities only). `apps/api/app/execution/persistence.py`'s
  `load_paper_broker()`/`save_paper_broker()` reconstruct/save a
  `PaperBrokerAdapter` around each trade request, with
  `load_paper_broker()` taking a `SELECT ... FOR UPDATE` lock on the
  account row held for the entire request to prevent a double-spend race
  on concurrent trades against the same broker.
  `submit_trade_and_record()` no longer commits internally (flushes only)
  so that lock survives until the route's single final commit. The old
  in-process `PaperBrokerRegistry` (D009) is deleted entirely, closing
  the D005/D009 gap for real — a restart no longer resets any account. 3
  new tests, 86/86 total passing, ruff+mypy clean (39 source files).
  Verified live in the way that actually matters here: submitted a trade,
  killed the running server process, started a fresh one, submitted a
  second trade on the same broker — the resulting cash balance
  (99,000 − 250 = 98,750) and both positions (one from before the
  restart, one from after) were only explainable if state genuinely
  survived the restart.
- Phase 12: Longbridge market data provider (2026-08-23). Closes D008.
  `apps/api/app/marketdata/providers/longbridge.py`'s
  `LongbridgeMarketDataProvider` wraps the `longport` SDK (verified
  against the real installed package via direct introspection, not
  documentation, after two web sources gave conflicting method names).
  `build_longbridge_provider()` returns `None` unless all three
  `LONGPORT_APP_KEY`/`LONGPORT_APP_SECRET`/`LONGPORT_ACCESS_TOKEN`
  settings are configured — the app boots fine either way. New
  `GET /market-data/{symbol}/quote` (auth required) returns 503
  `NOT_CONFIGURED:` with no vendor wired, 404 `NO_DATA_AVAILABLE:` if the
  vendor has no data, 200 with a real quote otherwise. **Not** wired into
  trade-proposal construction — `POST /brokers/{broker_id}/trades` still
  takes price/timestamp from the caller, unchanged; whether/how to
  connect the two is left as an explicit open decision (D015). 8 new
  tests (all against a fake client — no real credentials exist in this
  environment to test the "configured" path end to end), 94/94 total
  passing, ruff+mypy clean (43 source files). Verified live: server boots
  and logs `market_data_vendor=NOT_CONFIGURED`, and the endpoint returns
  503 rather than any fabricated quote.
- Phase 13: user/role update and deactivate endpoints (2026-08-23).
  Closes D013's deliberately-cut scope. `PATCH /admin/users/{id}`
  (`is_active`, `role_id`) and `PATCH /admin/roles/{id}` (`description`,
  `permissions`) added to `apps/api/app/api/routes/admin.py`, both gated
  by the same `Permission.ADMIN`. Both use Pydantic's `model_fields_set`
  to tell "key omitted" (leave untouched) apart from "key explicitly
  null" (unassign `role_id`) — a plain `is not None` check can't make
  that distinction. Still no delete endpoints for either resource
  (deleting a row would orphan `orders.submitted_by_user_id`/
  `users.role_id`); `is_active=false` remains the only way to deactivate
  a user, and clearing `permissions` is how a role gets neutralized. 7
  new tests, 101/101 total passing, ruff+mypy clean (43 source files).
  Verified live against a running server, real Postgres: deactivated an
  already-logged-in user via PATCH and confirmed their existing token got
  401 on its very next request (not just after re-login), then separately
  PATCHed a role to grant `trade:submit:paper` and confirmed a different
  already-issued token for that role immediately cleared the permission
  check — both prove `get_current_user`'s live re-check (never a cached
  JWT claim) is what makes authorization changes take effect
  immediately.
- Phase 14: market data wired into trade submission (2026-08-24). Closes
  D015's explicitly-left-open question. `TradeSubmissionRequest.estimated_price`
  is now optional — supplied, it's authoritative exactly as every prior
  phase behaved and the vendor is never consulted; omitted,
  `POST /brokers/{broker_id}/trades` fetches one live `MarketSnapshot`
  from the same `MarketDataRouter` the read-only quote endpoint uses, and
  takes both price and `market_data_as_of` from it together (never a
  vendor price paired with a caller-chosen timestamp). No vendor wired is
  400 `NOT_CONFIGURED:`; no data for the symbol is 400
  `NO_DATA_AVAILABLE:` — the same no-fabrication sentinels used
  everywhere else in the codebase. 4 new integration tests (one proves
  the vendor is never called at all when a price is supplied, using a
  provider that raises if invoked), 105/105 total tests passing (104 in
  one run plus the fail-closed JWT-secret test re-verified in true
  isolation, since it needs the env var genuinely unset), ruff+mypy clean
  (43 source files). Verified live against a running server, real
  Postgres, no Longbridge credentials configured: omitting
  `estimated_price` returned 400 `NOT_CONFIGURED`; supplying one still
  returned 200 filled exactly as before this phase. (Update 2026-08-24:
  real paper-trading Longbridge credentials were supplied and both this
  phase's and D015's live-quote paths were re-verified against genuine
  live data — see D015/D017's updates in `docs/DECISIONS.md`.)
- Phase 15: first agent, TraderAgent (2026-08-24). `apps/api/app/agents/`
  adds the first LLM-backed code in the codebase — a single
  single-responsibility agent (`docs/AGENT_POLICY.md`), not the full
  parallel-analyst/research-debate/portfolio-manager stack the governing
  spec eventually wants. `LLMProvider` is a narrow Protocol mirroring
  `MarketDataProvider`'s shape; `AnthropicCompatibleProvider` talks to any
  endpoint speaking the Anthropic Messages API (OmniRoute is the intended
  one) and is deliberately provider-name-agnostic
  (`LLM_PROVIDER_BASE_URL`/`_API_KEY`/`_MODEL`, not `OMNIROUTE_*` —
  `docs/MODEL_ROUTING.md`). `TraderAgent.propose()` takes a symbol and a
  free-text directive and returns a validated `TradeIdea`
  (side/quantity/stop_distance_pct/rationale) — **no price field at
  all**. New `POST /brokers/{broker_id}/agent-trades`
  (`apps/api/app/api/routes/trades.py`) combines that idea with the same
  live-quote path D017 uses for price, converts the stop distance into a
  real stop price deterministically, and submits through the identical
  `submit_trade_and_record()` → Risk Engine path a human-submitted trade
  uses — no separate or weaker validation for agent-originated trades.
  Malformed/unparseable LLM output is 502 `AGENT_OUTPUT_INVALID:`, never
  a fabricated trade; no provider configured is 400 `NOT_CONFIGURED:`,
  same convention as every other optional vendor in this codebase. 13 new
  tests (8 unit against a fake `LLMProvider`, 5 integration against real
  Postgres — including one proving an oversized agent-proposed quantity
  is rejected by the Risk Engine and not the agent, and one proving a
  missing broker grant returns 403 before the agent is ever called, using
  a provider that raises if invoked), 118/118 total tests passing,
  ruff+mypy clean (47 source files). OmniRoute was unreachable in this
  environment at implementation time (connection refused on
  `127.0.0.1:20128`), so only the NOT_CONFIGURED path was verified live —
  the "provider actually returns a usable completion" path is verified
  only against a fake provider in tests, the same limitation D015
  originally had for Longbridge before real credentials existed.
- Phase 16: first analyst, TechnicalAnalyst (2026-08-27). Closes the
  first slice of the parallel analyst layer per the governing spec's
  "Target" architecture — one analyst, not the full technical/
  fundamental/news/sentiment team, since only one has a real data source
  wired (Longbridge live quotes; `packages/data_providers/` remains an
  empty placeholder, so fundamental/news/sentiment would mean fabricating
  a feed, forbidden per spec §57). **Superseded in part by Phase 44/D059**,
  which found that the same already-credentialed Longbridge SDK also
  exposes company fundamentals and news, and built those two analysts on
  it; sentiment remains blocked. `TechnicalAnalyst.analyze()` reads the
  same live quote `agent-trades` already resolves and returns a
  `TechnicalRead` (`stance`/`summary`/`confidence`) — no price/side/
  quantity field, so nothing here can be mistaken for a trade proposal.
  It computes no indicator itself (real indicators must stay
  deterministic per `docs/TOKEN_POLICY.md` once real historical price
  data exists — it doesn't yet). Wired as optional context into
  `TraderAgent.propose()`'s prompt on `POST /brokers/{broker_id}/agent-trades`
  — absent or failed, the trade proceeds exactly as before this phase, no
  context appended, never blocked. `agent-trades` now resolves the live
  quote before calling the trader agent (was: agent first) so the
  analyst and trader share one quote; this real behavior change broke an
  existing D018 test that hadn't stubbed a market-data router, fixed by
  stubbing one to match its siblings. 10 new tests (7 unit against a fake
  `LLMProvider`, 3 integration against real Postgres — including proof
  via a `CapturingTraderAgent` subclass that the analyst's read actually
  reaches the trader's prompt, and proof that a failing analyst never
  blocks the trade), 128/128 total tests passing, ruff+mypy clean (49
  source files). Verified live against a running server, real Postgres,
  no `LLM_PROVIDER_*` configured: app booted logging
  `technical_analyst=NOT_CONFIGURED`, and `POST /brokers/{id}/agent-trades`
  returned 400 `NOT_CONFIGURED` rather than any fabricated read or trade.
  OmniRoute was unreachable at implementation time, same limitation as
  D018 — the "analyst returns a usable read" path is verified only
  against a fake provider.

- **Phase 17 — frontend, first slice (D020).** `apps/web/` — Next.js 16
  (App Router), TypeScript, Tailwind v4. `/login` posts to a Next.js route
  handler (`/api/auth/login`) that forwards OAuth2 form-encoded credentials
  to the backend and, on success, stores the JWT in an httpOnly cookie —
  the browser's own JS never touches the token. `/dashboard` (gated by
  `middleware.ts` on cookie presence) renders three real backend
  round-trips, each proxied through its own route handler that attaches
  `Authorization: Bearer <token>` server-side: `GET /health` status,
  a quote-lookup form against `GET /market-data/{symbol}/quote` (renders
  the actual 503 `NOT_CONFIGURED:`/404 `NO_DATA_AVAILABLE:` detail string
  on failure, never a generic message), and a paper-trade submission form
  against `POST /brokers/{broker_id}/trades` (renders the full
  `TradeSubmissionResponse` — `status`/`approved`/`block_reason`/
  `fill_price`/`fill_quantity` — including a rejected trade's
  `block_reason` legibly). No fabricated data anywhere: every fetch
  failure renders a real error state. Deliberately out of scope for v1:
  `/admin/*` UI, `/brokers/{id}/agent-trades` UI, broker discovery (no
  such endpoint existed then — built in Phase 29/D034), session refresh. `npm run build` succeeds with
  zero TypeScript errors. Test strategy: Vitest + React Testing Library
  (7 component tests covering quote success/503/404/network-failure and
  trade rejected/filled/403 rendering) — chosen over Playwright for this
  phase's scope (no real browser E2E flow to protect yet, and the app has
  no complex client-side state machine that unit/component tests can't
  already cover); Playwright is a reasonable v2 addition once there's a
  login → dashboard → trade flow worth protecting end-to-end. Verified:
  `npm run build` (zero type errors), `npm run dev` + curl against the
  Next.js app's own pages/routes. (Update 2026-08-28: re-verified
  end-to-end against a real running backend — a real login set the
  httpOnly cookie, a quote lookup returned the backend's genuine 503
  `NOT_CONFIGURED:`, and a trade submission returned a genuine `filled`
  response with a real fill price, all through the frontend's own proxy
  routes. See D020's update for full detail.)
- Phase 18: real historical prices for TechnicalAnalyst (2026-08-28).
  Closes D019's "future work" gap. `apps/api/app/marketdata/history_provider.py`
  adds a `HistoryProvider` Protocol (a price series, distinct from
  `MarketDataProvider`'s single quote); `LongbridgeHistoryProvider`
  implements it via the same Longbridge SDK connection (D015) using
  `candlesticks()`, verified against the real installed package by
  introspection. `apps/api/app/marketdata/indicators.py` adds pure,
  deterministic `sma()`/`rsi()` — no LLM, per `docs/TOKEN_POLICY.md`'s
  mandatory-deterministic list. `TechnicalAnalyst` (D019) can now narrate
  real computed indicator values when history is available — its prompt
  still explicitly forbids calculating or inventing one itself. Wired
  into `POST /brokers/{broker_id}/agent-trades`: optional, never blocking
  — a short history, vendor failure, or unconfigured provider all just
  mean no indicator context that call. 18 new tests (9 indicator unit
  tests, 5 unit tests against a fake candlestick client, 4 integration
  tests against real Postgres), 145/145 total tests passing, ruff+mypy
  clean (51 source files). One real bug caught during this phase's own
  test run and fixed: `rsi()`'s `zip(..., strict=True)` was wrong for its
  naturally-unequal-length consecutive-pair slices, raising on every real
  call. Verified live against the real Longbridge API directly (real
  paper-trading credentials): fetched 30 real daily closes for `AAPL.US`
  and computed genuine `SMA(20)=309.3215`/`RSI(14)=51.57...` — a level of
  verification D019 couldn't reach at the time. Also verified against a
  running server with no credentials configured: startup logged
  `history_provider=NOT_CONFIGURED`, `agent-trades` correctly 400s on
  D018's LLM-provider gate first.
- Phase 19: first Portfolio module (2026-08-28, D022). Closes
  `docs/MODULE_MAP.md`'s Portfolio gap. `apps/api/app/portfolio/` adds
  `compute_portfolio_snapshot()` — deterministic, no LLM, no writes —
  reading `broker_accounts`/`broker_positions` for current cash/quantity
  and replaying `orders`/`fills` per symbol for average-cost-basis
  `avg_cost`/realized P&L (`replay_symbol_fills()`, a pure function).
  `GET /brokers/{broker_id}/portfolio` exposes it, gated by a new
  `Permission.VIEW_PORTFOLIO` (deliberately separate from
  `SUBMIT_PAPER_TRADE` — read-only reporting shouldn't require trade
  rights) plus the usual `require_broker_access` grant check; current
  marks travel as a JSON body (`{"marks": {...}}`) on the GET, same shape
  as `TradeSubmissionRequest.marks`. A missing mark for a held position is
  400 `DATA_UNAVAILABLE:`, never a guessed price. 16 new tests (6 pure
  unit tests hand-verifying the average-cost replay across a
  buy/buy/sell/sell sequence, 10 integration tests against real Postgres
  reusing `tests/api/test_trades.py`'s fixtures), 169/169 total tests
  passing, `ruff check .`/`mypy apps` clean (one pre-existing, unrelated
  `B905` finding in `indicators.py` predated this phase — fixed
  independently in the concurrent Phase 22 below). Verified live
  against a real running server (`docker compose -p trading-os-phase19`,
  remapped host ports to avoid a sibling worktree's port clash): a
  no-trade broker reports starting cash and empty positions; a real
  `AAPL` buy followed by a marked portfolio call returned the
  hand-computed quantity/avg_cost/current_value/unrealized_pnl exactly;
  omitting the mark for that held position correctly 400'd
  `DATA_UNAVAILABLE:`. Explicit non-scope, not built: backtesting,
  alerts, performance-attribution-over-time (all need persisted
  historical snapshots — a bigger decision for a future phase), and
  FIFO/LIFO cost basis (see D022's alternatives; subsequently revisited
  and built in Phase 34/D041, which derives lots from the `orders`/`fills`
  history rather than adding per-lot state).
- Phase 22: deterministic duplicate-order detection in the Risk Engine
  (2026-08-28, D024). Closes the `docs/PROJECT_CONTEXT.md` "Open
  Decisions" gap D004 flagged as not built. A duplicate is defined as a
  proposal matching a FILLED order on the same broker (symbol, side,
  quantity, estimated_price all equal) within a 5s window — REJECTED
  orders are deliberately excluded (see D024). `apps/api/app/risk/models.py`
  adds `RecentOrder` and `RiskLimits.duplicate_order_window_seconds`;
  `evaluate_trade()` gains an optional `recent_orders` parameter and stays
  fully I/O-free — the caller (`trades.py`'s `_execute_trade()`, shared by
  both the human and agent-trades routes) queries
  `oms/persistence.py`'s new `get_recent_filled_orders()` and hands the
  result in as plain data. A new composite index
  `ix_orders_broker_symbol_submitted` (migration 0007) backs that query.
  New `BlockReason.DUPLICATE_ORDER`. 14 new tests (9 pure unit tests in
  `tests/risk/test_engine.py`, 1 in `tests/oms/test_service.py`, 1
  DB-backed in `tests/db/test_order_persistence.py`, 2 DB-backed HTTP
  integration tests in `tests/api/test_trades.py`, 1 DB-backed HTTP
  integration test in `tests/api/test_agent_trades.py`), 159/159 total
  tests passing, ruff+mypy clean (also fixed one pre-existing, unrelated
  ruff finding in `marketdata/indicators.py`). Verified live against a
  running server with a directly-inserted user/role/broker/grant: an
  identical `AAPL buy 10 @ 100` paper trade submitted twice ~0.5s apart —
  first filled, second rejected with `block_reason=duplicate_order`; a
  third submission with `quantity=5` right after filled normally,
  confirming no over-firing. All verification rows and infra (venv,
  `.env`, Docker containers) removed afterward.
- **Phase 20 — frontend v2: admin UI and agent-trades UI (D023).** Builds
  the two "v2 candidates" Phase 17/D020 explicitly deferred, in
  `apps/web/`, on the same route-handler-proxy pattern (no new auth
  mechanism). Admin UI (`/admin`, gated by `proxy.ts` on cookie presence
  only, same as `/dashboard`): six forms covering every `/admin/*`
  endpoint — create/update user, create/update role, create/revoke broker
  grant — each behind its own route handler
  (`app/api/admin/users(/[userId])`, `app/api/admin/roles(/[roleId])`,
  `app/api/admin/broker-grants(/[grantId])`). The dashboard's "Admin" nav
  link is always shown regardless of the viewer's actual permissions —
  deliberate: the real `admin:manage` gate is the backend's 403 on
  submit, and a client-side guess about permissions the client can't
  verify would be its own kind of fabrication. A non-admin submitting any
  admin form sees the backend's real 403 detail string. Agent-trades UI:
  `AgentTradeForm` on the dashboard, posting to
  `POST /brokers/{broker_id}/agent-trades` via
  `app/api/agent-trades/[brokerId]/route.ts`; renders the full
  `AgentTradeResponse` (`side`/`quantity`/`rationale` plus the existing
  trade-response fields) and the endpoint's two real error sentinels —
  400 `NOT_CONFIGURED:` and 502 `AGENT_OUTPUT_INVALID:` — never a
  fabricated trade. Deliberately out of scope, unchanged from D020:
  broker-discovery UI (no such endpoint), session refresh/expiry UX,
  Playwright e2e. `npm run build` succeeds with zero TypeScript errors.
  `npm test`: 28/28 passing (7 pre-existing + 21 new component tests
  across 4 new test files, covering success, a real 403, a real 404/409
  where applicable, and network failure for every new form). Verified
  live against a real running backend in this worktree (`docker compose
  up -d --build`, host ports for Postgres/Redis temporarily remapped only
  to avoid colliding with sibling Phase 19/21 worktrees, restored
  afterward): ran real Alembic migrations, bootstrapped one admin
  user/role via direct SQL (bcrypt hash, same pattern as D013/D016/D021),
  then through `npm run dev` + curl against the frontend's own routes —
  real 201s creating a role and a user, a real 200 assigning the role via
  PATCH, a real 200 updating the role's description via PATCH, a real 201
  granting broker access against a SQL-inserted paper broker, a real 204
  revoking it followed by a real 404 on repeating the delete, a real 403
  `Missing required permission: admin:manage` from the new non-admin user
  attempting an admin action, and a real 400
  `NOT_CONFIGURED: no LLM provider is wired` from that same non-admin
  user's agent-trade attempt (no `LLM_PROVIDER_*` configured in this
  pass). `AGENT_OUTPUT_INVALID:` (502) was exercised only via a mocked
  component test, not a real misbehaving provider. See D023 for full
  detail.
- **Phase 24 — frontend portfolio view (D026).** Builds the "portfolio
  view" v2/v3 candidate D020/D023 both deferred, against Phase 19/D022's
  `GET /brokers/{broker_id}/portfolio`. `PortfolioView`
  (`components/PortfolioView.tsx`) on `/dashboard` takes a broker UUID
  plus a `SYMBOL=price` marks string and renders the real
  `PortfolioSnapshot` — `cash`, a table of every position's
  `symbol`/`quantity`/`avg_cost`/`current_value`/`unrealized_pnl`/
  `realized_pnl`, and the three totals — a single real-time snapshot,
  deliberately no historical chart (needs Phase 25's persisted
  snapshots). Backed by `app/api/portfolio/[brokerId]/route.ts`, exposed
  to the browser as `POST` but internally issuing the real backend call
  as a `GET` carrying `marks` as a JSON body, per D022's contract. Node's
  `fetch` (undici) cannot send a body on GET at all — `lib/backend.ts`
  gained `backendGetWithBody()`, which uses Node's core `http`/`https`
  module directly for this one call instead. `npm run build` succeeds
  with zero TypeScript errors. `npm test`: 34/34 passing (28 pre-existing
  + 6 new in `test/PortfolioView.test.tsx`: a real rendered snapshot, an
  empty-positions render, the 400 `DATA_UNAVAILABLE:` sentinel for a
  missing mark, a 403, a 404, and network failure). Verified live against
  a real running backend in this worktree (`docker compose up -d
  --build`, host ports temporarily remapped to avoid colliding with
  sibling Phase 23/25 worktrees, restored afterward): ran real Alembic
  migrations against a fresh database, bootstrapped two users via direct
  SQL (one with `portfolio:view` + a broker grant, one with the grant but
  no `portfolio:view`), submitted a real trade that filled
  (`fill_price: "100"`, `fill_quantity: "10"`), then through `npm run dev`
  + curl against the frontend's own `/api/portfolio/[brokerId]` route —
  a real 200 rendering that exact filled position and its computed
  totals from a supplied mark of `120`, a real 400
  `DATA_UNAVAILABLE: No mark supplied for open position 'AAPL.US'; cannot
  value account.` when the mark was omitted, a real 404 for an unknown
  broker, a real 401 with no session cookie, and a real 403
  `Missing required permission: portfolio:view` from the second seeded
  user. See D026 for full detail.

---
- **Phase 25 — persisted portfolio snapshots (2026-08-28, D027).** Closes
  the "persisted historical portfolio snapshots" item Phase 19/D022
  explicitly deferred. Two new append-only tables (migration
  `0008_portfolio_snapshots.py`; models `PortfolioSnapshotRow`/
  `PortfolioSnapshotPositionRow` in `apps/api/app/db/models.py`) - a
  parent `portfolio_snapshots` row (cash/total_equity/total_unrealized_pnl/
  total_realized_pnl/captured_at) plus a child `portfolio_snapshot_positions`
  row per open position (chosen over a JSON column for queryability - see
  D027). `POST /brokers/{broker_id}/portfolio/snapshots` computes a
  snapshot via the existing `compute_portfolio_snapshot()` (same
  `{"marks": {...}}` body, same `DATA_UNAVAILABLE:` missing-mark
  discipline) and persists it; `GET /brokers/{broker_id}/portfolio/history`
  returns persisted snapshots for a broker ordered oldest-to-newest by
  `captured_at`, paginated (`limit` default 50/max 500, `offset` default
  0). Both gated by the existing `Permission.VIEW_PORTFOLIO` (see D027 for
  why no new permission was needed) plus `require_broker_access`.
  Deliberately manual-only - no cron/scheduler was built; a snapshot only
  ever exists because a caller explicitly POSTed it. 8 new integration
  tests against real Postgres in `tests/api/test_portfolio.py` (23 total
  in that file), plus `tests/api/test_trades.py`'s shared
  `paper_broker_row` fixture extended to clean up the two new tables on
  teardown. 188/188 total tests passing, `ruff check .`/`mypy apps` both
  clean (58 source files). Verified live against a running Docker Compose
  stack in this worktree: seeded a role/user/broker/grant via direct SQL
  (bcrypt hash), submitted a real `AAPL` buy, POSTed a real snapshot with
  a real mark (response showed the real position and P&L), then GET
  `.../history` and confirmed both the pre-trade and post-trade snapshots
  came back in the correct order with values matching exactly. All seeded
  rows, the Docker stack, the local venv, and `.env` were removed
  afterward.

- **Phase 26 — trade-path Portfolio Manager (2026-08-29, D029).** Builds the
  "Portfolio Manager" box in docs/ARCHITECTURE.md's Target diagram / the
  "Portfolio Decision" step in docs/TRADING_SAFETY.md's pipeline — the
  component D022 and D027 both explicitly flagged as *not* what
  `apps/api/app/portfolio/` is. New package
  `apps/api/app/portfolio_manager/` (`models.py`, `manager.py`): a pure
  `decide(proposal, portfolio, limits) -> PortfolioDecision` with no LLM,
  no network call and no I/O, mirroring the Risk Engine's own zero-I/O
  discipline. Wired into `submit_trade()`
  (`apps/api/app/oms/service.py`) AFTER `evaluate_trade()` approves a
  proposal and BEFORE the broker call — `apps/api/app/risk/engine.py`
  itself is unmodified. Two structural properties, each with a dedicated
  test: a risk-rejected proposal never reaches the Portfolio Manager
  (it is a second gate, not a bypass), and a MODIFY-resized proposal is
  re-run through `evaluate_trade()` before any broker call, so no
  quantity this system produces reaches a broker ungated. Three
  constraints, all computable from data the repo actually stores:
  `symbol_concentration` (post-trade value of one symbol vs equity,
  default 25% — the aggregate-position gap no per-trade check sees),
  `cash_reserve` (post-trade cash vs equity, default 5%), and
  `max_open_positions` (distinct held symbols, default 20, labelled as a
  count, not as diversification). Spec §18's correlation, sector
  concentration, portfolio volatility, expected return and drawdown are
  NOT implemented — no sector column on `assets` and no persisted
  per-symbol return series exist, and approximating them would be
  fabrication (see D029). `REQUEST_MORE_RESEARCH` exists in the enum for
  spec fidelity but is never emitted; a test pins that. A failing check
  only blocks when the trade *worsens* that measure, so a de-risking sell
  is never refused because of the breach it relieves. Audit record per
  spec §18: migration `0009_orders_portfolio_decision.py` adds four
  nullable `orders` columns (`portfolio_action`,
  `portfolio_binding_constraint`, `portfolio_detail`,
  `portfolio_requested_quantity`); `orders.quantity` is now the quantity
  actually acted on, so a resize shows as `quantity !=
  portfolio_requested_quantity` rather than a rewritten proposal, and a
  null `portfolio_action` means "never ran", never "approved".
  `TradeSubmissionResponse` surfaces the same four fields, which makes
  `approved: true` + `status: "rejected"` a real combination (risk passed,
  portfolio didn't). 26 new tests — 13 pure unit
  (`tests/portfolio_manager/test_manager.py`), 5 OMS-wiring
  (`tests/oms/test_service_portfolio_manager.py`), 4 real-Postgres
  persistence (`tests/db/test_portfolio_decision_persistence.py`), 4
  real-Postgres HTTP (`tests/api/test_trades_portfolio_manager.py`) —
  for 234/234 total passing, `ruff check .`/`mypy apps` clean (66 source
  files). Verified live against a real Docker Compose stack in this
  worktree (host ports remapped to 5435/6382/8002 via a throwaway
  override so it could run alongside sibling Phase 27/28 worktrees; the
  tracked `docker-compose.yml` was never edited), with real Alembic
  migrations `0001`→`0009` against a fresh database: real curl requests
  to the containerized API produced, on one broker, two `approve` fills,
  a real `modify` cutting a 98-share buy to 51 with
  `binding_constraint: symbol_concentration`, a real `reject` at the
  exact 25% cap (`approved: true`, `block_reason: null`), and a
  de-risking sell correctly approved anyway — with Postgres confirming
  the resized row (`quantity 51`, `portfolio_requested_quantity 98`), 250
  shares held and 75,000 cash, i.e. the resized trade's arithmetic and
  not the requested one. Not verified live: no market-data vendor or LLM
  provider was configured, so all prices were caller-supplied and the
  agent-trades route (which shares the same `_execute_trade()` path) was
  exercised only against a fake `LLMProvider`; `apps/web/` was not run or
  updated and still renders only the risk verdict; `apps/api/app/
  backtesting/` calls `evaluate_trade()` directly, so backtests do not
  model portfolio constraints. Docker containers/volumes, the compose
  override, the test-only `.env` and the venv were removed afterward.

- **Phase 27 — automatic/scheduled portfolio snapshots (2026-08-29, D030).**
  Closes the "automatic/scheduled portfolio snapshotting" item Phase
  25/D027 explicitly deferred, and adds the first background component in
  the system. `apps/api/app/portfolio/scheduler.py`: a
  `PortfolioSnapshotScheduler` (plain `asyncio` task + `asyncio.sleep`, no
  new dependency) started from the FastAPI lifespan, running one cycle
  immediately and then every `PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS`
  (new setting, default 3600). Gated by
  `PORTFOLIO_SNAPSHOT_SCHEDULER_ENABLED` (new setting, **default false** -
  see D030 for the fail-closed reasoning); with the defaults, nothing is
  constructed and runtime behavior is unchanged from Phase 25. A cycle
  reads the brokers that have a `broker_accounts` row, and for each one
  fetches a real quote for every nonzero-position symbol through the
  *existing* `MarketDataRouter` (D015/D017) - the scheduler has no caller
  to supply `marks`, and it never invents, defaults, or carries one
  forward. If any held symbol has no real quote, that broker is skipped
  for that cycle: nothing is written, and a typed
  `ScheduledSnapshotOutcome` (`CAPTURED` /
  `SKIPPED_MARKET_DATA_UNAVAILABLE` /
  `SKIPPED_MARKET_DATA_NOT_CONFIGURED` / `SKIPPED_NO_BROKER_ACCOUNT` /
  `SKIPPED_INCOMPLETE_VALUATION`) is returned and logged at warning level
  with the unpriced symbols and the vendor's own sentinel text. A
  cash-only broker (no open positions) needs no marks and is captured
  normally even with market data entirely NOT_CONFIGURED. When it does
  capture, it runs the identical `compute_portfolio_snapshot()` and the
  identical write path the manual `POST .../portfolio/snapshots` uses -
  that write was extracted this phase into
  `apps/api/app/portfolio/persistence.py`
  (`persist_portfolio_snapshot()`), with the route's response-shaping
  pulled into a `_to_history_entry()` helper, so the two paths cannot
  drift; a scheduler-written row reads back through
  `GET .../portfolio/history` indistinguishably from a manual one. No new
  table and no migration were needed. 20 new tests (8 unit in
  `tests/portfolio/test_scheduler.py` - mark resolution through a real
  `MarketDataRouter` with a fake only at the vendor boundary, plus the
  interval and default-off guards; 12 DB-backed integration in
  `tests/api/test_snapshot_scheduler.py`, reusing
  `tests/api/test_trades.py`'s fixtures, covering capture, each skip
  reason, "one unpriceable symbol blocks the whole snapshot", a full
  mixed cycle, and the running task actually writing a row on its timer).
  228/228 total tests passing, `ruff check .`/`mypy apps` both clean
  (65 source files). Verified live against a real uvicorn process and a
  real Postgres in this worktree with the scheduler enabled at a 10s
  interval: two seeded brokers, both given real positions through the
  real trade endpoint. The flat (bought-then-sold) broker was
  snapshotted automatically every cycle, and `.../portfolio/history`
  returned four genuine scheduler-written rows (cash `100100.00000000`,
  realized P&L `100.00000000`, `captured_at`s ~10s apart) computed from
  the real fills; the broker still holding `AAPL.US` was skipped on every
  single cycle with `status: "skipped_market_data_not_configured"` and
  `unpriced_symbols: ["AAPL.US"]`, its history staying empty - the
  no-fabrication guarantee observed live rather than only asserted in
  tests. **Not verified live:** the router-supplied-mark *capture* path
  against a real vendor - no Longbridge credentials exist in this
  environment, so the market-data layer genuinely reported
  NOT_CONFIGURED and the live run exercised the refusal path, not the
  success path; that path is covered only by the integration tests
  (real router, fake at the vendor boundary). Graceful lifespan shutdown
  (`scheduler.stop()`) was covered by tests but not in the live run,
  which ended in a forced kill. Docker containers/volumes, a temporary
  port-remapping compose override (needed to run alongside concurrent
  phase-26/phase-28 stacks), the test-only `.env`, and the verification
  venv were all removed afterward.

- **Phase 28 — frontend v3: historical performance chart, admin listing UI
  + its backend endpoints, session expiry UX (2026-08-29, D031/D032).**
  Closes three of the four frontend candidates Phase 24/D026 left open.
  (1) **Historical performance chart.** `PortfolioHistoryChart`
  (`apps/web/components/PortfolioHistoryChart.tsx`) on `/dashboard`
  charts `total_equity` over `captured_at` from Phase 25/D027's
  `GET /brokers/{id}/portfolio/history`, backed by a new
  `app/api/portfolio/[brokerId]/history/route.ts` proxy. The chart is a
  hand-rolled inline SVG polyline — **no new npm dependency was added**;
  every plotted vertex is one real returned snapshot, nothing is
  interpolated, smoothed, back-filled, or extended past the last real
  capture, and the accompanying table prints each snapshot's raw values
  verbatim. The x axis is by snapshot index, not elapsed time, because
  D027 capture is manual and a time-scaled axis would imply a sampling
  cadence that does not exist. Zero snapshots renders "no snapshots have
  been captured", never an empty or zeroed curve.
  (2) **Users/roles/grants listing UI + the backend endpoints it needed
  (D031).** `GET /admin/users`, `GET /admin/roles`, and
  `GET /admin/broker-grants` were added to
  `apps/api/app/api/routes/admin.py` — read-only, no new permission
  (they inherit the router's existing `admin:manage` dependency),
  paginated with the same `limit`(50/500)/`offset` convention as D027's
  history endpoint, each with a fixed total sort. Rows reuse the existing
  `CreateUserResponse`/`CreateRoleResponse`/`BrokerGrantResponse` DTOs,
  so no password or hash can appear. `UsersList`/`RolesList`/
  `BrokerGrantsList` (`apps/web/components/admin/AdminListings.tsx`) on
  `/admin` render them; the grants table surfaces the `id` the revoke
  form needs, which previously required a direct DB query.
  (3) **Session refresh/expiry UX (D032).** New `GET /auth/session`
  returns `user_id`/`email`/`issued_at`/`expires_at`/
  `expires_in_seconds` — never the token, never a renewed one, and 401
  for an expired token or a deactivated user. It exists because the JWT
  is in an httpOnly cookie (D020) that client JS cannot read, so a
  "session expiring soon" warning was otherwise impossible without
  fabricating a local countdown. `SessionStatus` polls it once a minute
  and warns below 5 minutes; it never ticks a local timer down between
  polls. A shared `apps/web/lib/session.ts` `handleExpiredSession(status)`
  is now called at the `!res.ok` branch of every authenticated component,
  so any route handler's 401 redirects to `/login?reason=session-expired`
  where a real explanation renders instead of a bare "HTTP 401".
  **Backend:** `ruff check .` and `mypy apps` clean (64 source files);
  **226/226 pytest tests passing** (208 pre-existing + 18 new: 12 in
  `tests/api/test_admin_listings.py`, 6 in `tests/api/test_session.py`),
  all against real Postgres. **Frontend:** `npm run build` with zero
  TypeScript errors; **61/61 Vitest tests passing** (34 pre-existing + 27
  new across `test/PortfolioHistoryChart.test.tsx`,
  `test/AdminListings.test.tsx`, `test/SessionStatus.test.tsx` — success,
  empty-result, real error sentinels, 403, 404, 401-redirect, and
  network-failure paths for every new component, plus pure-function tests
  for the chart's coordinate mapping and the remaining-time formatter).
  **Verified live** against a real Docker Compose stack in this worktree
  (host ports remapped to 5436/6383/8003 to avoid the sibling phase26
  worktree's stack, removed afterward): real Alembic migrations, a seeded
  admin user/role/broker/grant, a real filled `AAPL.US` trade, and four
  real POSTed snapshots producing a genuinely varying equity curve
  (100000 → 100050 → 99980 → 100120). Exercised every new endpoint by
  curl both directly against the API and through the frontend's own route
  handlers with a real login cookie: all three listings, the history
  endpoint, `/auth/session`, plus `limit=501` → 422, `offset=-1` → 422,
  no token → 401, and a non-admin's real 403 on all three listings. In a
  real browser: logged in through the real UI, loaded the history chart
  (4 real plotted vertices with correct geometry), saw all three admin
  listings render the real seeded rows, saw `SessionStatus` show the
  backend's real "29m left", and — after deactivating that user directly
  in Postgres — watched the next `/admin` load redirect to
  `/login?reason=session-expired` with the real message.
  **Not done:** Playwright/e2e coverage, deliberately skipped because it
  requires a new tool/vendor dependency that was outside this phase's
  no-new-dependency scope; it remains open pending explicit permission.
  See D031/D032 for full detail.

- **Phase 30 — backtests gated by the Portfolio Manager too (2026-08-29,
  D035).** Closes the honest gap D029 itself recorded: the backtest engine
  called `evaluate_trade()` directly, so backtests modelled per-trade risk
  but no portfolio-level constraint. `apps/api/app/backtesting/engine.py`
  now replicates `oms/service.py::submit_trade()`'s RISK ENGINE ->
  PORTFOLIO MANAGER -> BROKER sequence inline, on every simulated trade
  decision: a risk-approved proposal goes to the real
  `portfolio_manager.manager.decide()`, and a MODIFY's resized quantity is
  passed back through `evaluate_trade()` before the simulated fill — the
  same load-bearing re-gate invariant D029 pinned for the live path. The
  `PortfolioState` is built at each step from the run's own disposable
  in-memory `PaperBrokerAdapter` (D025), never from a real broker's rows,
  and `submit_trade()` itself is deliberately NOT called because it is
  DB/audit-coupled while a backtest writes nothing. The loop stays pure and
  deterministic — no LLM, no I/O added. `POST /backtests` supplies the same
  `Settings.portfolio_*` limits the live trade path uses (no
  backtest-specific override), and `BacktestResult` gains
  `portfolio_modified_trades`, `portfolio_modify_risk_blocked_trades` and
  `portfolio_rejected_trades` so the audit trail exists in backtests too.
  **277/277 pytest tests passing** (272 pre-existing + 5 new in
  `tests/backtesting/test_engine_portfolio_manager.py`), `ruff check .` and
  `mypy apps` clean (69 source files). **Verified live** over real HTTP
  against this worktree's own Docker stack (ports remapped to 5443/6390/8030
  to avoid the running sibling phase29 stack; torn down afterwards): real
  Alembic `0001`->`0009`, a seeded user, a real login, and three real
  `POST /backtests` runs showing approve-only, a real MODIFY, and a real
  portfolio REJECT. **Not verified with real vendor data:** no Longbridge
  credentials were readable in this session, so the containerized API
  honestly returned `NOT_CONFIGURED` for a real-symbol run and the live
  runs above used an injected fake `HistoryProvider`'s synthetic closes —
  unlike D025, which was verified against real Longbridge history. See D035.

- **Phase 32 — Portfolio Manager verdict in the web UI (2026-08-29, D038).**
  Closes D029's own "Consequences" gap: `apps/web/`'s trade forms showed
  the Risk Engine's verdict only, never the Portfolio Manager's, even
  though `approved: true` + `status: "rejected"` (a portfolio rejection)
  is a real, easily-misread response combination. New
  `components/PortfolioVerdict.tsx` renders alongside — never replacing —
  the existing risk verdict in both `TradeForm.tsx` and
  `AgentTradeForm.tsx`: a `modify` shows the binding constraint, the
  backend's detail string, and both quantities (requested vs. actually
  filled); a `reject` renders visually and textually distinct from a risk
  rejection (different color/label/icon, so the two rejection sources are
  never conflated); `approve` or a `null` action (Portfolio Manager never
  ran, e.g. risk-rejected first) render no extra panel at all — a null
  action is never shown as if it approved anything. Also fixed a
  pre-existing bug found in passing: the risk block's color was keyed on
  `status`, so a portfolio rejection was painted the Risk Engine's amber;
  it is now keyed on `approved`, so amber always means the Risk Engine
  said no. **No backend change.** **79/79 frontend tests passing** (69
  pre-existing + 10 new, 5 per form: approve/modify/reject/null-action/
  older-shaped-response), `npm run build` clean with zero TypeScript
  errors. **Verified live** against this worktree's own Docker stack
  (ports remapped to 5438/6388/8008; torn down afterwards; the user's own
  dev stack and sibling phase31/33 worktrees confirmed untouched): four
  real trade submissions on one real broker (approve, a real modify via a
  tightened `PORTFOLIO_MAX_SYMBOL_PCT_OF_EQUITY`, a real portfolio reject,
  and a real risk-engine reject) fed verbatim into the real components,
  confirming each renders the correct panel/color. **Not verified live:**
  a full click-through of the running dev server itself — the browser
  tool in this environment could not load its JS chunks, so React never
  hydrated; the real-payload-into-real-component check above substitutes
  for it. No market-data vendor or LLM was wired, so `AgentTradeForm` was
  exercised only through its own tests. See D038.

- **Phase 31 — backtest frontend (2026-08-29, D037).** Closes the gap D035
  recorded verbatim in its own "Not verified live" note: `apps/web/` did not
  render the backtest response at all, let alone the new Portfolio Manager
  counters. Adds `app/api/backtests/route.ts` (the same route-handler-proxy
  + httpOnly-cookie pattern every other authenticated feature uses, minus a
  `brokerId` segment because `POST /backtests` is deliberately not
  broker-scoped per D025), `components/BacktestPanel.tsx`, and one line
  wiring the panel into `/dashboard`. Renders the real `BacktestResult`:
  a hand-rolled inline-SVG equity curve in the same style as
  `PortfolioHistoryChart.tsx` (no new npm dependency, one vertex per real
  trading day, index x-axis, flat curves drawn flat), the five headline
  metrics, and all three D035 counters — `portfolio_modified_trades`,
  `portfolio_modify_risk_blocked_trades`, `portfolio_rejected_trades` —
  never collapsed into one number, always shown even at zero, with a caption
  stating that Risk Engine rejections are a separate gate counted by none of
  them and that all-zero does not by itself mean everything was approved.
  Every real error sentinel is surfaced verbatim and no error path ever
  draws a curve or counters. **No backend change was needed or made.**
  **89/89 frontend tests passing** (69 pre-existing + 20 new in
  `test/BacktestPanel.test.tsx`), `npm run build` clean with zero TypeScript
  errors. **Verified live** against this worktree's own Docker stack (ports
  remapped to 5451/6391/8031; torn down afterwards) with `npm run dev` on
  3031: real Alembic `0001`->`0009`, a seeded user, a real browser login,
  and real submissions through the real UI rendering
  `HTTP 400: NOT_CONFIGURED: no history provider.` and
  `HTTP 422: body: Value error, end_date must be after start_date`, plus a
  real 401 from the route handler with no cookie.
  **Not verified live:** the success path — no market-data vendor
  credentials were readable in this session (the same limitation D025/D035
  recorded), so no real equity curve, real metrics, or real non-zero
  counters could be produced. Success-path rendering, and the
  `UNSUPPORTED_DATE_RANGE:`/`DATA_UNAVAILABLE:` sentinels (both unreachable
  live behind the `NOT_CONFIGURED` guard), are covered by component tests
  against mocked responses only. See D037.

- **Phase 33 — persisted, audited, live-flippable emergency stop
  (2026-08-29, D039).** Closes PROJECT_CONTEXT.md's long-standing
  "emergency-stop *source* is undecided" open item. Spec §46's kill switch
  was an `.env` value that required a restart to flip — the exact thing its
  own docstring said it must not require. It is now an append-only
  `emergency_stop_events` table (migration `0010`): one row per flip with
  `active`, a required `reason`, the acting `actor_user_id`, and
  `created_at`; the current state is the highest-`id` row, so state and
  audit trail are the same object. Three endpoints in
  `apps/api/app/api/routes/emergency_stop.py`:
  `POST /admin/emergency-stop` and `POST /admin/emergency-stop/deactivate`
  (both `admin:manage`, both requiring a non-blank reason) and
  `GET /admin/emergency-stop` (authentication only — knowing you are halted
  is not privileged, same scoping argument as D034). **The Risk Engine is
  unchanged and still zero-I/O:** `evaluate_trade()` still takes
  `emergency_stop_active: bool` as a plain parameter, and the DB read
  happens one layer up in `_execute_trade()` (shared by the human and agent
  trade routes, a single read site), with the new persistence deliberately
  in a new `apps/api/app/safety/` package rather than inside `risk/` — a
  test asserts that boundary structurally.
  `Settings.emergency_stop_active` survives only as the bootstrap default
  used while the table is empty; once a row exists it is never consulted.
  **304/304 pytest tests passing** (288 post-Phase-29/30-merge baseline +
  16 new in `tests/api/test_emergency_stop.py`), `ruff check .` and
  `mypy apps` clean (74 source files). **Verified live** against this
  worktree's own Docker Postgres/Redis (ports remapped to 55433/56380 to
  avoid the user's own dev stack; torn down afterwards): real
  `alembic upgrade head` `0001`→`0010`, then against a real `uvicorn`
  process — a real trade filled, the stop activated over HTTP with a real
  reason, the next real trade rejected with `emergency_stop_active` in the
  same process with no restart, then the **process killed and restarted**
  with a byte-identical `.env` (md5-checked) and the state confirmed still
  active and still blocking real trades, then deactivated and a real trade
  filled again. See D039.
- **Phase 34 — selectable FIFO/LIFO cost basis alongside average-cost
  (2026-08-30, D041).** Closes IMPLEMENTATION_STATUS's own "FIFO/LIFO
  cost-basis reporting as a Portfolio module alternative" planned item,
  which D022 had deferred. D022's stated blocker — "this schema has no
  per-lot data" — was true of stored *state* but not of history:
  `orders`/`fills` already record every individual buy with its own
  quantity, price and `filled_at`, which is exactly a lot ledger, so the
  lots are **derived, not invented, and no migration is needed**.
  `apps/api/app/portfolio/models.py` adds `CostBasisMethod`
  (`average`/`fifo`/`lifo`); `snapshot.py` splits the replay into
  `replay_symbol_fills_average()` (verbatim D022) and
  `replay_symbol_fills_lots()` (a signed open-lot tracker; one
  `newest_first` flag is the only difference between FIFO and LIFO, so
  they cannot drift apart), behind an unchanged `replay_symbol_fills()`
  that still defaults to AVERAGE. Both are pure — no DB, no I/O, no LLM.
  `GET /brokers/{broker_id}/portfolio` gains an optional
  `?cost_basis_method=` query param; **omitting it is byte-identical to
  the D022 response** (a test asserts full response equality, not just
  matching numbers), and an unrecognised value is a 422, never a silent
  fallback. **Deliberate non-scope at the time:** the persisted-snapshot
  POST (D027) and the scheduler (D030) remained average-cost only —
  `portfolio_snapshots` recorded no method column, so a stored FIFO row
  would have been indistinguishable from an average one in
  `GET .../history`. **Superseded by Phase 36/D044**, which added that
  column and opened the write path. **317/317 pytest tests passing** (304 D040-confirmed
  merged baseline + 13 new: 10 pure lot-math unit tests in
  `tests/portfolio/test_snapshot.py`, 3 DB-backed HTTP tests in
  `tests/api/test_portfolio.py`), `ruff check .` and `mypy apps` clean (74
  source files). **Verified live** against this worktree's own Docker
  Postgres/Redis and a rebuilt API container (ports remapped to
  5442/6389/8010 to avoid the user's dev stack and the sibling Phase 35
  worktree; torn down afterwards): a real seeded multi-lot history — buy
  10 @ 100, buy 10 @ 110, sell 15 @ 120, marked at 130 — returned over
  real HTTP `average` basis 105 / realized 225 / unrealized 125, `fifo`
  110 / 250 / 100, and `lifo` 100 / 200 / 150, each matching the
  hand-computed figure, with `cash` 99700 and `total_equity` 100350
  identical across all three and `?cost_basis_method=hifo` rejected 422.
  See D041.

- **Phase 36 — `cost_basis_method` recorded on every persisted snapshot
  (2026-08-30, D044).** Closes the single tracked follow-on D041 left
  open. Migration `0011_portfolio_snapshots_cost_basis_method.py` adds
  `cost_basis_method VARCHAR(16) NOT NULL DEFAULT 'average'` to
  `portfolio_snapshots` — the column whose absence was D041's stated
  reason for keeping the write path average-only.
  `POST /brokers/{broker_id}/portfolio/snapshots` now takes an optional
  `cost_basis_method` **in its request body** (default `average`, same
  `average`/`fifo`/`lifo` enum, 422 on anything else with nothing
  written), threads one variable into both `compute_portfolio_snapshot()`
  and `persist_portfolio_snapshot()` so the recorded method cannot
  disagree with the numbers, and echoes it back.
  `GET /brokers/{broker_id}/portfolio/history` surfaces it on every entry,
  so a FIFO row's realized P&L can no longer be misread as average-cost.
  The scheduler (D030) gains the same capability via a new
  `portfolio_snapshot_cost_basis_method` setting that **defaults to
  `average` — its behaviour is unchanged unless explicitly configured**,
  and is typed as the real enum so a typo fails at app startup.
  **Backward compatibility is the load-bearing constraint:** every row
  written before the migration reads `average` from the column's
  `server_default`, never null — that is a recorded fact (the write path
  was average-only until now), not a stand-in for "unknown" — and a test
  INSERTs a row omitting the column entirely, asserting both the raw
  Postgres value and the HTTP response read `average`.
  **359/359 pytest tests passing** (349 D043-confirmed merged baseline +
  10 net new: 6 in `tests/api/test_portfolio.py`, which also replaced
  D041's now-obsolete `test_persisted_snapshots_stay_average_cost_...`
  guard against the very behaviour this phase adds; 3 DB-backed scheduler
  tests in `tests/api/test_snapshot_scheduler.py`; 2 settings tests in
  `tests/test_config.py`). `ruff check .` and `mypy apps` clean (75 source
  files). **Verified live** against this worktree's own Docker
  Postgres/Redis and a rebuilt API container (ports remapped to
  5452/6399/8020 via an untracked compose override to avoid the user's dev
  stack and the sibling Phase 37 worktree; torn down afterwards): all
  eleven migrations clean from empty with `\d portfolio_snapshots`
  confirming the NOT NULL default, then over real HTTP on a real seeded
  multi-lot history (buy 10 @ 100, buy 10 @ 110, sell 15 @ 120, marked at
  130) the default POST stored and read back `average` 105 / 225 / 125,
  `fifo` 110 / 250 / 100 and `lifo` 100 / 200 / 150 — each matching
  D041's already-verified hand-computed figures — with `cash` 99700 and
  `total_equity` 100350 identical across all three, history naming each
  row's method, `hifo` rejected 422, and a column-less INSERT reading back
  `average`. See D044.

- Phase 44: FundamentalAnalyst + NewsAnalyst (2026-09-01). Closes the
  "no real data source" block that kept these two unbuilt since Phase 16.
  No new vendor: direct introspection of the already-installed, already-
  credentialed `longport` 4.3.7 SDK found company fundamentals
  (`AsyncFundamentalContext.company()`/`.valuation()`) and news
  (`AsyncContentContext.news()`) alongside the quotes and candlesticks
  D015/D021 already use — same three `LONGPORT_*` variables, no new
  service, no new credentials. Adds two ports
  (`marketdata/fundamentals_provider.py`, `marketdata/news_provider.py`),
  their Longbridge implementations, one pure-deterministic metrics module
  (`marketdata/fundamental_metrics.py`: exact `100/PE` earnings yield and
  latest-point-by-timestamp selection — the LLM computes nothing), and the
  two analysts. Both return the same `stance`/`summary`/`confidence`
  contract as `TechnicalRead` with no price/side/quantity field. Wired as
  optional, additive context into `POST .../agent-trades` exactly like
  D019 — no new endpoint, no new request/response field, no new error
  response; all three analysts now run concurrently and are independently
  failure-isolated. A missing vendor metric renders as "not reported by
  the vendor", never a zero or an estimate; the news prompt's headline
  count and date range are computed in code so the model cannot overstate
  its coverage. `SentimentAnalyst` deliberately NOT built (see D059).
  481 tests pass (57 new). **Not verified against a real vendor response:**
  no `LONGPORT_*` credentials exist in this environment, so the provider
  layer is verified only against fakes at the vendor boundary plus SDK
  introspection — a first live run remains outstanding. See D059.

## In Progress

Nothing currently mid-implementation.

## Blocked

Nothing currently blocked.

## Planned

Phase 20+ (order not finalized): the rest of the parallel analyst layer —
fundamental and news are now DONE (Phase 44/D059, built on the existing
Longbridge SDK's fundamentals and news endpoints); **sentiment remains
blocked** on a real sentiment data source, since the SDK exposes no
sentiment score and having the LLM invent one from headlines is exactly
what spec §57 forbids (see D059's scope cut). Research debate is still
planned. FIFO/LIFO cost-basis reporting is now DONE (Phase
34/D041), and its one tracked follow-on - offering the choice on the
write path, which needed a migration recording which method produced each
row - is now DONE too (Phase 36/D044). No cost-basis follow-on remains.
Automatic/scheduled
portfolio snapshotting is now DONE (Phase 27/D030) - remaining follow-ons
there: market-hours awareness is now PARTIALLY addressed (Phase 35/D042
gates whole cycles on a UTC weekend; intraday after-hours and exchange
holidays are still NOT checked, and closing that needs a real
trading-calendar port over the Longbridge SDK's `trading_session()` /
`trading_days()`, plus a market-timezone source this repo does not have -
see D042's Alternatives for the three concrete blockers and the
fail-open requirement). Multi-worker safety is now DONE (Phase 38/D047:
a non-blocking Postgres advisory lock per cycle, so >1 uvicorn/gunicorn
worker yields one row per interval instead of one per worker); no
scheduler follow-on remains apart from the intraday/holiday half of
market-hours awareness above. If a second analyst is
ever added, revisit whether
parallel-execution infrastructure across analysts is now warranted
(deliberately not built in Phase 16 — one analyst has nothing to
parallelize against). Frontend candidates remaining after Phase 20/D023
and Phase 24/D026, now further reduced by Phase 28/D031/D032 (the
historical performance chart, the users/roles/grants listing UI, and
session expiry UX are all built) and by Phase 29/D034 (broker discovery,
backend and UI): Playwright e2e coverage — deliberately skipped in
Phase 28 because it needed a new tool dependency — is now DONE too
(Phase 37/D045, 11/11 specs passing against a real backend). No frontend
candidates remain. Re-verify D018/D019's LLM-completion path
once OmniRoute (or another Anthropic-Messages-API-compatible endpoint) is
reachable — D021 confirmed the Longbridge/history side works with real
data, but no real LLM completion has been exercised yet, only fakes.

## Technical Debt

None yet — codebase is small enough that debt hasn't accumulated.

## Optimization Tasks

Deferred until an agent/LLM layer exists — see [AI_OPTIMIZATION.md](AI_OPTIMIZATION.md).
Building a token router/cache with nothing to route would be premature
optimization.

## Completed (continued)

- Phase 23: backtesting engine (2026-08-28). `apps/api/app/backtesting/`
  (`strategy.py`, `metrics.py`, `models.py`, `errors.py`, `engine.py`) — a
  single hard-coded SMA(20)-crossover strategy replayed against real
  historical closes (`HistoryProvider`, D021) through the real Risk
  Engine (`evaluate_trade`, D004) and a fresh in-memory
  `PaperBrokerAdapter` per run (D014) — never a real broker's persisted
  state. `POST /backtests` (`api/routes/backtests.py`, gated by
  `get_current_user` alone, no `broker_id`/`Permission`) returns a
  `BacktestResult` (equity curve, total return, trade count, win rate,
  max drawdown — every number hand-verified in tests). See
  docs/DECISIONS.md D025 for the strategy/scope/permission reasoning and
  the `HistoryProvider`-imposed `end_date == today` limitation. 33 new
  tests (21 pure unit — metrics/strategy/engine — + 7 HTTP integration +
  5 already counted via reused fixtures), 202/202 total tests passing,
  ruff+mypy clean (67 source files). Live-verified against real
  Longbridge paper-trading credentials: a real `AAPL.US` backtest over
  the most recent ~45 days produced a genuine, non-trivial equity curve
  with one real trade filled and risk-gated exactly as the unit tests
  predict.

- Phase 29: broker discovery (2026-08-29). `GET /brokers` and
  `GET /brokers/{broker_id}` (`apps/api/app/api/routes/brokers.py`,
  `api/schemas_brokers.py`) — authentication-only, scoped to the calling
  user's own `BrokerGrant` rows, so a non-admin trader can finally learn
  which broker ids they may trade on instead of being handed a UUID
  out-of-band. Paginated `limit`(50/500)/`offset` exactly as D027/D031;
  detail route returns 404 for a nonexistent broker and 403 for one the
  caller holds no grant on, matching `require_broker_access`; response is
  a five-field allow-list (`id`, `name`, `kind`, `provider`,
  `is_active`), never a credential-shaped field. Frontend:
  `components/BrokerDiscovery.tsx` + `app/api/brokers/route.ts` (same
  httpOnly-cookie route-handler proxy as every other feature), with a
  per-row "Use" button that pre-fills the real broker id into the trade,
  agent-trade, portfolio and portfolio-history forms via
  `lib/brokerSelection.ts` — a convenience only; the backend still
  re-checks the grant on every request. See docs/DECISIONS.md D034 for
  the grants-define-discoverability scoping decision and the rejected
  alternatives (reusing the `admin:manage` grant listing, a global broker
  listing with an `accessible` flag, permission-gating, 403-on-no-grants).
  11 new backend integration tests (`tests/api/test_brokers.py`) against
  real Postgres + 8 new Vitest tests
  (`apps/web/test/BrokerDiscovery.test.tsx`); **283 backend tests
  passing**, **69 web tests passing**, `ruff check .` and `mypy apps`
  clean (71 source files), `npm run build` clean. Live-verified against a
  running stack: two seeded users with disjoint grants each saw only
  their own broker; cross-user detail 403, unknown id 404, no token 401.

- Phase 35: market-hours gating for the snapshot scheduler (2026-08-30).
  `apps/api/app/portfolio/market_hours.py` — a frozen, I/O-free
  `MarketHoursGate` whose `evaluate(as_of)` returns a typed
  `MarketHoursDecision` (`RUN` / `RUN_GATE_DISABLED` / `SKIP_WEEKEND`).
  `run_snapshot_cycle()` (D030) evaluates it **before** the eligible-broker
  query, so a gated cycle costs zero SQL and zero market-data vendor
  calls; `SnapshotCycleResult` gained a `market_hours` field and a `gated`
  property, and `PortfolioSnapshotScheduler` gained a `market_hours_gate`
  and an injectable `clock` read once per cycle. New setting
  `PORTFOLIO_SNAPSHOT_MARKET_HOURS_GATE_ENABLED`, **default true** (the
  safe side here is the side that does less work), wired in `main.py` and
  surfaced in the startup log line. No new dependency, table, migration,
  or HTTP surface.
  **Scope is weekends only, and deliberately so.** This partially closes
  D030's recorded gap ("the interval is wall-clock, not market-hours-aware
  ... will keep recording unchanged after-hours rows"). Saturday/Sunday is
  a property of the calendar, not of any exchange's policy, so it is
  computable without a vendor; intraday session times and exchange
  holidays are NOT checked, because doing that honestly needs a real
  trading-calendar source. The installed `longport` v4.3.7 SDK *does*
  expose `trading_session()` and `trading_days()` on `AsyncQuoteContext`
  (confirmed by direct introspection, not assumed), but wiring them needs
  a market→timezone map the SDK does not supply, a symbol→`Market`
  mapping, and — for `market_status()` — a second SDK context this
  codebase has never constructed. That is a phase, not a footnote; see
  docs/DECISIONS.md D042, which also records that a market-calendar pip
  dependency was deliberately NOT added and that a hardcoded holiday/hours
  table was rejected as fabrication under docs/TRADING_SAFETY.md / spec
  §57.
  32 new tests (20 pure unit in `tests/portfolio/test_market_hours.py`,
  12 DB-backed in `tests/api/test_snapshot_scheduler_market_hours.py`);
  **336 backend tests passing** in this worktree over a measured 304
  baseline, `ruff check .` and `mypy apps` clean (75 source files).
  Live-verified against a real uvicorn process and real Postgres on a real
  UTC **Sunday**: gate on → 12 consecutive `portfolio_snapshot_cycle_gated`
  events, zero captures, zero rows for a genuinely eligible seeded broker;
  same process and broker with the gate off → 6 real captures at
  `total_equity = 100100`. Weekday behavior in tests uses an **injected**
  Monday instant, not a real one.

- Phase 41: production readiness — a real readiness probe and a
  multi-stage, non-root API image (2026-08-31, D054). **(1) `GET /health`
  was fake.** It reported `"status": "ok"` from in-process `Settings`
  alone and never checked whether the app could reach Postgres, so an
  instance with an unreachable database advertised itself as healthy and
  an orchestrator would have kept routing traffic to it. `/health` is now
  explicitly the **liveness** probe (unchanged body, no I/O — deliberately
  stays 200 during a database outage, because restarting a process does
  not fix a down database and a dependency-checking liveness probe would
  crash-loop every replica). The new **`GET /health/ready`** runs a real
  `SELECT 1` through the app's own engine, bounded by
  `HEALTH_READINESS_TIMEOUT_SECONDS` (default 3.0), and returns 503 with a
  fixed-vocabulary `reason` (`connection_failed` / `timeout`) and the same
  body shape as its 200. Exception *type names* only, never messages — a
  DSN error carries the password and this endpoint is unauthenticated.
  **No Redis check**: Redis is provisioned but still unwired in any Python
  code (re-confirmed this phase), so checking it would be fabricated
  signal. **(2) `apps/api/Dockerfile` was single-stage and ran as root.**
  Now `builder` (deps → `/opt/venv`) + `runtime` (venv, app code,
  `packages/`, `migrations/`, `alembic.ini`), `USER appuser` before `CMD`,
  pip/setuptools/wheel removed from both the venv and `/usr/local`. Port,
  CMD, and env-var contract unchanged; no new dependency; no migration.
  Image **373MB → 354MB** (an intermediate version was briefly *larger*
  at 387MB — see D054). **404 backend tests passing** over the measured
  400 baseline, `ruff check .` clean, `mypy apps` clean (78 source files,
  77 before). Live-verified by actually running the rebuilt image: `whoami`
  → `appuser`, `alembic current` → `0012 (head)` from inside it, and the
  full probe cycle against a real isolated Postgres — healthy 200/200,
  Postgres stopped → `/health` still 200 while `/health/ready` → 503
  `reason: "timeout"`, Postgres back → `/health/ready` 200 again with the
  API container never restarted.

- Phase 40: CI hardening — a frontend job, two long-standing CI-breaking
  bugs fixed, migration round-trip confirmed sound (2026-08-31, D052).
  **(1) `apps/web` now has CI at all.** A second `web` job in
  `.github/workflows/ci.yml` runs `npm ci` → `npm run build` → `npm test`
  (Vitest) on Node 22, `working-directory: apps/web`, with npm caching
  keyed on `apps/web/package-lock.json`. It declares no services and no
  env because none of those commands need Postgres, Redis, or the API, and
  it does not `needs:` the backend job so neither can mask the other's
  failure. Twenty-plus phases of frontend work had no automated coverage
  on push or PR before this. **(2) The secret-scan step had been failing
  every build since Phase 1/7.** It inlined its own regex into the
  workflow and then `git grep`-ed the repo for it, so it matched
  `ci.yml` itself plus the deliberate `sk-live-`-shaped fixture in
  `tests/test_logging.py`, and exited 1 before `ruff` ever ran. The scan
  moved to `scripts/secret_scan.sh` (new), which excludes only itself by
  path and skips lines marked `pragma: allowlist secret` — per line, not
  per file. **(3)
  `tests/test_config.py::test_missing_jwt_secret_key_fails_closed` was
  failing under CI's own env.** `Settings(_env_file=None)` does not
  suppress the process environment, and the CI job exports
  `JWT_SECRET_KEY` for every step, so the fail-closed assertion ran with
  the key present; the test now `monkeypatch.delenv`s it. **(4) The
  migration downgrade round-trip is genuinely fine** — verified against a
  real TimescaleDB/pg16 instance, twice, with `psql` confirming
  `downgrade base` leaves only `alembic_version` and zero enum types. No
  migration was changed. Deliberately out of scope and recorded as such in
  D052: the Playwright e2e suite is **not** wired into CI (needs a live
  backend, seeded Postgres, and a browser install — a bigger job that
  could not be verified CI-equivalent locally), and `npm run lint` is
  **not** in the web job (it is currently red: two
  `react-hooks/set-state-in-effect` errors in
  `apps/web/components/SessionStatus.tsx`, one Next warning in
  `apps/web/lib/session.ts`). No new CI service, SaaS, or secret was
  introduced. Verified live, every CI step run locally with its exact
  command against a private compose stack on remapped ports: ruff clean,
  mypy 77 files clean, migrations both directions, **400 pytest tests
  passing** (399/1 before fix 3), **102 Vitest tests across 13 files
  passing**, `npm run build` green with all 22 routes.

- Phase 39: security-audit remediation — login lockout, CSRF posture on
  record, vitest CVE (2026-08-30). Fixes three findings from a completed
  read-only security review of the backend and frontend.
  **(1) Failed-login lockout on `POST /auth/login`** (D049), the app's one
  previously unthrottled credential endpoint: `apps/api/app/auth/lockout.py`
  (new, pure arithmetic with a single `utcnow()` clock seam) + two new
  `users` columns (`failed_login_count`, `locked_until`; migration `0012`)
  + two new settings (`AUTH_MAX_FAILED_LOGIN_ATTEMPTS` default 5, `0`
  disables; `AUTH_LOCKOUT_DURATION_MINUTES` default 15, both validated at
  startup). N consecutive failures lock the account; a correct password
  during the lock gets **423** with a clear message, while a wrong password
  still gets the generic **401** — so 423 is unreachable to anyone who
  doesn't already know the password and adds no email-enumeration oracle on
  top of the existing `_DUMMY_HASH` timing parity. No new pip dependency and
  no first-ever Redis client: Postgres was already a hard dependency of this
  route, and unlike an in-process counter it survives restarts and is
  correct under `uvicorn --workers N`.
  **(2) CSRF posture recorded** (D050): documentation only — `SameSite=Lax`
  on D020's httpOnly cookie remains the sole defence, which is sound
  precisely because no route in this app changes state on a GET. Written up
  in `docs/API.md` so the invariant it rests on is visible to whoever adds
  the next route.
  **(3) vitest 2 → 4** in `apps/web` (D050), clearing a critical
  `vitest --ui` advisory (never used here, dev-only, never shipped) plus
  transitive `vite`/`esbuild` ones: `npm audit` **5 → 0 vulnerabilities**;
  `@vitejs/plugin-react` to v6 and `vitest.config.ts` → `.mts` with
  `import.meta.dirname` were the real config migrations required, not
  suppressed warnings.
  **400 backend tests passing** over the measured 379 baseline (+21: 9
  pure-arithmetic, 8 real-Postgres integration, 4 config-validation),
  `ruff check .` and `mypy apps` clean (77 source files); **102 Vitest
  tests passing** over the 99 baseline (+3 `LoginForm` tests pinning that
  the 423 message reaches the user verbatim), `npm run build` succeeds.
  Live-verified against a real uvicorn + real Postgres: real HTTP POSTs
  drove a real lockout (401 ×3 → 423 on the correct password → 401 on a
  wrong one), the `users` row showed the real counter and timestamp, and
  the lock cleared on the **real wall clock** with a deliberately short
  1-minute configured window. The 15-minute default's expiry is covered by
  the injected-clock tests, not walked in real time.

- Phase 38: multi-worker safety for the portfolio snapshot scheduler
  (2026-08-30). `apps/api/app/portfolio/cycle_lock.py` (new) +
  `PORTFOLIO_SNAPSHOT_CYCLE_LOCK_ENABLED` (new setting, **default true**)
  + `cycle_lock=` on `run_snapshot_cycle()` and
  `PortfolioSnapshotScheduler`, wired in `main.py`. Closes the last open
  consequence D030 recorded: the scheduler's lifespan-owned loop runs once
  per worker process, so `uvicorn --workers N` used to append N
  near-simultaneous rows per interval to an **append-only** table. A cycle
  now takes a non-blocking session-level
  `pg_try_advisory_lock(1953653092, 1936613744)` — two fixed int4 keys
  (ASCII `trad`/`snap`), not `hashtext()`, so the pair is stable across
  Postgres major versions and greppable in `pg_locks` — before enumerating
  any broker, and releases it with `pg_advisory_unlock` in a `finally` on
  every path including an exception. A worker that cannot get it records
  the new typed `SnapshotCycleLockDecision.SKIPPED_LOCK_HELD` (an ordinary
  outcome, logged at info, **not** an error) and skips without one broker
  query or one vendor call. The lock is taken *after* D042's weekend gate,
  so a gated weekend cycle still costs zero DB round-trips.
  **No new dependency**: Redis is provisioned in `docker-compose.yml` and
  named by `Settings.redis_url` but is still unwired in Python as of this
  phase, and Postgres is the one service the cycle cannot run without
  anyway — so the lock adds nothing that can fail independently of the
  work it guards. No new table and no migration either: a session-level
  advisory lock dies with its connection, so a SIGKILLed worker cannot
  wedge the schedule. Single-worker behaviour is unchanged (the lock is
  always acquired) and `=false` restores D030's exact unguarded path.
  **379/379 pytest tests passing** (359 D046-confirmed baseline + 20 new:
  8 unit in `tests/portfolio/test_cycle_lock.py`, 2 settings tests in
  `tests/test_config.py`, 10 DB-backed in
  `tests/api/test_snapshot_scheduler_multiworker.py` where the contending
  "other worker" is a genuinely separate real session really holding the
  real lock). `ruff check .` and `mypy apps` clean (76 source files).
  **Verified live** against this worktree's own Docker Postgres (isolated
  compose project `tos38`, ports remapped to 55432/56379 so the user's dev
  stack on 5432/6379 was untouched; torn down afterwards) with
  `scripts/seed_e2e.py` fixtures: **two separate OS processes** each
  running one real cycle, released at the same instant against the same
  database — with the lock on, one process captured and the other logged
  `portfolio_snapshot_cycle_lock_not_acquired` and returned
  `skipped_lock_held`, leaving **1 row**; the identical race with the lock
  off produced **2 rows**, reproducing the pre-D047 bug live as a control.
  See D047.

- Phase 37: Playwright e2e coverage for `apps/web/` (2026-08-30).
  `apps/web/playwright.config.ts` + `apps/web/e2e/` (`fixtures.ts`,
  `helpers.ts`, `global-setup.ts`, `auth.spec.ts`, `trade.spec.ts`,
  `broker-discovery.spec.ts`, `admin.spec.ts`) and `scripts/seed_e2e.py`
  — the last remaining frontend candidate, unblocked by the user's
  explicit permission for the one new dependency (`@playwright/test`
  plus Chromium only, not all three engines). Run by a new
  `npm run test:e2e`; deliberately NOT part of `npm test`, and
  `vitest.config.ts` now excludes `e2e/**` so Vitest never collects specs
  that cannot run without a database. Nothing is mocked: real Chromium →
  real `next dev` → real route handlers → real FastAPI → real Postgres
  with real seeded rows, and real `orders`/`fills` written by the real
  Risk Engine (D004) and Portfolio Manager (D029). The backend URL is
  configurable (`E2E_API_BASE_URL`, default `http://localhost:8000`), as
  is the dev-server port (`E2E_WEB_PORT`, default 3100). Covered: valid
  login → `/dashboard` + a real httpOnly cookie invisible to
  `document.cookie`; invalid login → the backend's real
  `Incorrect email or password.`, no redirect, no cookie; no cookie →
  proxy bounce to `/login`; an invalid session on a protected page →
  `/login?reason=session-expired` (D032); a real approved trade with a
  real fill and, per D038, no Portfolio Manager panel; a real
  `missing_stop_price` risk rejection; a real Portfolio Manager MODIFY
  (requested 100 / filled 50, binding constraint `symbol_concentration`)
  beside a non-amber risk verdict; a real 403 on an ungranted broker;
  `BrokerDiscovery` listing real granted brokers and pushing one into the
  trade and agent-trade forms; and `/admin` listing real users/roles/
  grants for an admin while a non-admin gets three real 403s. **11/11
  specs passing, verified three times consecutively** against a real stack
  (compose project `tradingos-e2e37` on ports 55437/56437, all eleven
  migrations applied, real uvicorn on 8037, real Chromium) — the user's
  own dev stack on 5432/6379/8000/3005 was left untouched. Not covered,
  deliberately: every flow needing a real LLM provider or market-data
  vendor (agent trades, quotes, portfolio views, backtests) since neither
  is configured here, and the Portfolio Manager's REJECT /
  `max_open_positions` paths, which `TradeForm` cannot reach because it
  has no "marks" input. See docs/DECISIONS.md D045 (D044 was claimed by
  the parallel phase-36 worktree).

## Tests

Phases 43, 44, and 45 (live-trading path, FundamentalAnalyst/NewsAnalyst,
dashboard redesign) were built in three parallel worktrees off the same
D057 `main` and independently confirmed **487** (Phase 43, 424 baseline +
63 new) and **481** (Phase 44, same 424 baseline + 57 new) — disjoint new
test files from two branches that both touched
`apps/api/app/api/dependencies.py`. After merging all three (43 → 44 → 45,
resolving an additive conflict in that same file plus `docs/DECISIONS.md`)
a full from-scratch `pytest tests/ -q` against a freshly migrated, isolated
Postgres/Redis stack (compose project `tosverifymain`, ports 55499/56499,
never touching the user's own running dev stack on 5432/6379/8000/3005)
collected the real merged total: **544 passed, 0 failed, 3 pre-existing
warnings** (424 + 63 + 57 = 544 exactly — confirms the two branches' new
tests were genuinely disjoint, not that any silently dropped or collided).
`ruff check .` and `mypy apps` both clean (86 source files); `bash
scripts/secret_scan.sh` clean; `alembic upgrade head` applied cleanly with
no new migration from any of the three phases. The live-trading safety
invariant was explicitly re-checked post-merge: `git grep` for
`live_trading_enabled\s*=\s*True|LIVE_TRADING_ENABLED=true` across `apps/`
and `tests/` returns only docstrings/error-message text and two
pre-existing unit tests that construct a `Settings` object to test the
fail-closed validator itself (`tests/core/test_execution_context.py`,
`tests/test_config.py`) — neither starts an app or attempts a broker call.
No code path anywhere sets this flag true and runs. See D061 for the full
verification record.

Each of Phase 26, Phase 27, and Phase 28 was built in its own parallel
worktree against the same 208-test baseline and independently confirmed a
real `pytest tests/ -q` collected count within its own worktree: **234**
for Phase 26 (208 baseline + 26 new — 13 pure unit tests for the
trade-path Portfolio Manager in `tests/portfolio_manager/test_manager.py`,
5 for its OMS wiring in `tests/oms/test_service_portfolio_manager.py`
including one proving a Portfolio-Manager-resized quantity is itself
re-gated by the Risk Engine, 4 real-Postgres persistence tests for the
`orders.portfolio_*` audit columns in
`tests/db/test_portfolio_decision_persistence.py`, and 4 real-Postgres
HTTP integration tests in `tests/api/test_trades_portfolio_manager.py`),
**228** for Phase 27 (208 baseline + 20 new — 8 unit in
`tests/portfolio/test_scheduler.py`, 12 DB-backed integration in
`tests/api/test_snapshot_scheduler.py` covering scheduled-snapshot
capture, each typed skip reason, and the running asyncio task writing a
real row on its timer), and **226** for Phase 28 (frontend-heavy;
`GET /auth/session` and the admin listing endpoints added their own
backend test files independently of the other two worktrees). None of
those three numbers was the real post-merge total, since each worktree
only saw its own disjoint additions layered on the shared 208 baseline,
not the other two phases' new tests combined.

That real merged total has since been confirmed: **272 tests, all
passing** (now **283** with Phase 29's broker-discovery tests), by a full from-scratch backend verification of the fully-merged
main branch (fresh venv, `ruff check .`, `mypy apps`, real Postgres/Redis
via `docker compose`, `alembic upgrade head` through migration 0009 —
`orders.portfolio_*` from Phase 26 — and `pytest tests/ -q`) run
2026-08-29 after Phases 26/27/28 were merged. `ruff` and `mypy` were both
clean and no cross-phase integration bug was found this time — see
docs/DECISIONS.md D033 for the full verification record and D028 for the
precedent this pass follows.

That 272/283 figure has itself since been superseded by a further
from-scratch verification confirming the true post-Phase-29/30-merge
total: **288 tests, all passing**, run 2026-08-29 against `main` at
`d7069e0` (Phases 29 broker-discovery and 30 backtest-Portfolio-Manager-
gating both merged), following the same D028/D033 precedent — fresh venv,
`ruff check .` (clean), `mypy apps` (clean, 71 source files), a
separately-project-named `docker compose` Postgres/Redis pair,
`alembic upgrade head` through migration 0009 (still the current head —
neither phase added a migration), and `pytest tests/ -q` against that real
database. This does not simply add Phase 29's independently-reported 283
to Phase 30's independently-reported 277 (off its own 272 baseline plus 5
new disjoint tests for the backtest-engine Portfolio Manager gating,
D035): 283 already **is** the correct merged Phase-26-through-29 total per
the paragraph above, and Phase 30 layered its own 5 new tests on top of
that same 283, giving 288 as the real collected count — confirmed live,
not computed. Zero failures, zero errors, 3 pre-existing warnings (same
two `InsecureKeyLengthWarning`s and one `StarletteDeprecationWarning` as
every prior run — neither new nor actionable). See docs/DECISIONS.md D036
for the full verification record.

Phase 33 (D039, this worktree) adds 16 new tests in
`tests/api/test_emergency_stop.py` on top of that confirmed 288 baseline,
for **304 collected and passing** in this worktree against a real Postgres.
As with every parallel-worktree count before it, 304 is this worktree's own
number: sibling phase-31/32 worktrees are adding their own disjoint tests
concurrently, so the true post-merge total must be re-derived from a
clean-room run against merged `main`, per the D028/D033/D036 precedent.

That re-derivation has since been done. A full from-scratch backend
verification run 2026-08-29 against `main` at `05e6cc2` (Phases 31
backtest-frontend, 32 portfolio-manager-UI, and 33 emergency-stop-admin
all merged) confirmed the true post-merge total is **304 tests, all
passing** — exactly Phase 33's own worktree count, because Phases 31 and
32 were frontend-only and added no backend tests on top of the 288
baseline. Following the same D028/D033/D036 precedent: fresh Python 3.13
venv, `pip install -e ".[dev]"`, `ruff check .` (clean, "All checks
passed!"), `mypy apps` (clean, "Success: no issues found in 74 source
files" — 74 vs. D036's 71 reflects Phase 33's new `safety/` module),
a separately-named throwaway Postgres/Redis pair on remapped host ports
(the user's own default-port 5432/6379 dev stack, plus their uvicorn on
8000 and Next.js dev server on 3005, was left completely untouched and
confirmed still running before and after), `alembic upgrade head` — all
ten migrations (0001 through Phase 33's new 0010
`emergency_stop_events`) applied cleanly in sequence against a real
Postgres 16 (timescaledb image) database with no manual intervention —
and `pytest tests/ -q` against that same real database: 304 passed, 3
pre-existing warnings (the same two `InsecureKeyLengthWarning`s and one
`StarletteDeprecationWarning` seen in every prior verification pass),
zero failures, zero errors. One transient failure surfaced on the first
attempt (`test_missing_jwt_secret_key_fails_closed`) — the same shell-
environment-leakage artifact documented in D036, this time from this
verification's own shell exporting `JWT_SECRET_KEY` ahead of the run;
re-running with only `DATABASE_URL` and `REDIS_URL` set reproduced the
clean 304-pass result with no code changes required. No real cross-phase
integration bug was found between Phase 33's DB-read emergency-stop
wiring (D039) and Phases 26-30's earlier trade-path work. Frontend
(`apps/web`) test file count was independently confirmed unchanged at
the 89-test / 12-file total reported after Phase 31 (D037): neither
Phase 32 nor Phase 33 added a new `apps/web/test/*.test.tsx` file. See
docs/DECISIONS.md D040 for the full verification record.

A full from-scratch backend verification run 2026-08-30 against `main` at
`ba941cb` (Phase 34 FIFO/LIFO cost-basis and Phase 35 market-hours
scheduler gate both merged) confirmed the true post-merge total is
**349 tests, all passing** — exactly the 304 D040-confirmed baseline plus
Phase 34's 13 new tests plus Phase 35's 32 new tests, since the two
phases' new test files are disjoint and neither phase's tests exercise
the other's code path. Following the same D028/D033/D036/D040 precedent:
fresh Python venv, `pip install -e ".[dev]"`, `ruff check .` (clean, "All
checks passed!"), `mypy apps` (clean, "Success: no issues found in 75
source files"), a separately-named throwaway Postgres/Redis pair on
remapped host ports 55432/56379 under its own compose project name
`tradingos-verify34-35` (the user's own default-port 5432/6379 dev stack,
plus their uvicorn on 8000 and Next.js dev server on 3005, was left
completely untouched and confirmed still running before and after),
`alembic upgrade head` — all eleven migrations (0001 through Phase 33's
`0010_emergency_stop_events`) applied cleanly in sequence against a real
Postgres 16 (timescaledb image) database with no manual intervention and
no new migration from either phase, as expected — and `pytest tests/ -q`
against that same real database: 349 passed, 3 pre-existing warnings (the
same two `InsecureKeyLengthWarning`s and one `StarletteDeprecationWarning`
seen in every prior verification pass), zero failures, zero errors. One
transient failure surfaced on the first attempt
(`test_missing_jwt_secret_key_fails_closed`) — the same shell-
environment-leakage artifact documented in D036 and D040, this time from
this verification's own shell exporting `JWT_SECRET_KEY` via its
throwaway `.env` file; re-running with that variable unset reproduced the
clean 349-pass result with no code changes required. No real cross-phase
integration bug was found between Phase 34's cost-basis method (D041) and
Phase 35's scheduler gate (D042). See docs/DECISIONS.md D043 for the full
verification record.

A full from-scratch backend verification run 2026-08-30 against `main` at
`034f4c3` (Phase 36 FIFO/LIFO-aware portfolio-snapshot cost-basis
persistence and Phase 37 frontend-only work both merged) confirmed the
true post-merge total is unchanged at **359 tests, all passing** — Phase
36's own report of 359, independently reconfirmed, since Phase 37 shipped
no backend changes and no new `tests/*.py` files. Following the same
D028/D033/D036/D040/D043 precedent: fresh Python venv, `pip install -e
".[dev]"`, `ruff check .` (clean, "All checks passed!"), `mypy apps`
(clean, "Success: no issues found in 75 source files"), a separately-named
throwaway Postgres/Redis pair on remapped host ports 15432/16379 under its
own compose project name `tos-verify` (the user's own default-port
5432/6379 dev stack, plus their uvicorn on 8000 and Next.js dev server on
3005, was left completely untouched and confirmed still running before
and after), `alembic upgrade head` — all eleven migrations (0001 through
Phase 36's `0011_portfolio_snapshots_cost_basis_method`) applied cleanly
in sequence against a real Postgres 16 (timescaledb image) database with
no manual intervention and no new migration beyond 0011, as expected —
and `pytest tests/ -q` against that same real database: 359 passed, 3
pre-existing warnings (the same two `InsecureKeyLengthWarning`s and one
`StarletteDeprecationWarning` seen in every prior verification pass), zero
failures, zero errors. One transient failure surfaced on the first
attempt (`test_missing_jwt_secret_key_fails_closed`) — the same shell-
environment-leakage artifact documented in D036, D040, and D043, this
time from this verification's own shell exporting `JWT_SECRET_KEY` via its
throwaway `.env` file; re-running with that variable unset reproduced the
clean 359-pass result with no code changes required. No real cross-phase
integration bug was found. A separate frontend check was also run:
`cd apps/web && npm install && npm run build && npm test` (Vitest unit
tests only, per this pass's scope — the new Playwright e2e suite added in
Phase 37 needs its own real backend/DB setup and was skipped here, already
verified live by Phase 37's own agent) — build succeeded, and **99 Vitest
tests, all passing**, exactly matching Phase 37's own report, unchanged.
See docs/DECISIONS.md D046 for the full verification record.

A full from-scratch backend verification run 2026-08-30 against `main` at
`b0040ea` (Phase 38's multi-worker snapshot-scheduler advisory lock
merged) confirmed the true post-merge total is **379 tests, all
passing** — exactly Phase 38's own independently-reported 379, since
Phase 38 added its new coverage on top of the 359 D046-confirmed
baseline and no other phase landed in between. Following the same
D028/D033/D036/D040/D043/D046 precedent: fresh Python venv, `pip install
-e ".[dev]"`, `ruff check .` (clean, "All checks passed!"), `mypy apps`
(clean, "Success: no issues found in 76 source files" — 76 vs. D046's 75
reflects Phase 38's new `apps/api/app/portfolio/cycle_lock.py`), a
separately-named throwaway Postgres/Redis pair on remapped host ports
15532/16479 under its own compose project name `tosverify38` (the user's
own default-port 5432/6379 dev stack, plus their uvicorn on 8000 and
Next.js dev server on 3005, was left completely untouched and confirmed
still running via `docker ps` and live HTTP checks against 8000/3005
before and after), `alembic upgrade head` — all eleven migrations (0001
through Phase 36's `0011_portfolio_snapshots_cost_basis_method`, still
the current head) applied cleanly in sequence against a real Postgres 16
(timescaledb image) database with no manual intervention and no new
migration from Phase 38, exactly as expected for an advisory-lock feature
that adds no schema — and `pytest tests/ -q` against that same real
database: 379 passed, 3 pre-existing warnings (the same two
`InsecureKeyLengthWarning`s and one `StarletteDeprecationWarning` seen in
every prior verification pass — neither new nor actionable), zero
failures, zero errors, in the clean run. One transient failure surfaced
on the first attempt (`test_missing_jwt_secret_key_fails_closed`) — the
same recurring D036/D040/D043/D046 shell-environment-leakage artifact:
this verification's own shell had exported `JWT_SECRET_KEY` ahead of
`alembic upgrade head` and the first `pytest` run, which leaked into that
one test's `Settings(_env_file=None)` construction; re-running with only
`DATABASE_URL`/`REDIS_URL` set (and `JWT_SECRET_KEY` unset) reproduced
the clean 379-pass result with no code changes required. No real
cross-phase integration bug was found between Phase 38's advisory-lock
cycle guard (D047) and any earlier phase's work. See docs/DECISIONS.md
D048 for the full verification record.


208 tests, all passing - confirmed 2026-08-29 by a full from-scratch
backend verification (fresh venv, `ruff check .`, `mypy apps`, real
Postgres/Redis via docker-compose, `alembic upgrade head` through 0008,
`pytest tests/ -q`) after the Phase 23/24/25 merge, correcting this
section's earlier 210-test placeholder estimate. That run also caught
one real cross-phase bug: `backtesting/engine.py`'s default "today" used
the raw calendar date instead of the most recent trading day, so any run
landing on a Saturday/Sunday rejected every request with
`UNSUPPORTED_DATE_RANGE` - fixed by rolling the default back to the prior
weekday (a caller-supplied `today`, as every existing test uses, is left
untouched). The count below is this verification's actual collected
total, not a per-phase arithmetic sum - see docs/DECISIONS.md D025/D027
for the per-phase new-test breakdowns that make up most of it.
Baseline, 174 tests: fail-closed live-mode gate (4, incl. missing JWT
secret), log redaction (1), health endpoint (1), risk engine (23, incl. 9
duplicate-order-detection tests — D024), paper broker (5), OMS (4, incl. 1
proving `recent_orders` blocks a duplicate before the broker is touched),
execution context (5), order/fill persistence (4, DB-backed, incl. 1
covering `get_recent_filled_orders()`), market data router + snapshot
model (9), Longbridge provider (8, against a fake client), trades HTTP
endpoint (20, DB-backed, all require auth+permission+broker grant — 14
pre-D017 + 4 covering the omitted-price/live-quote path + 2 covering
duplicate-order detection — D024), auth security unit tests (8),
authorization unit tests (3), login route (4, DB-backed), admin routes
(16, DB-backed — 9 create/grant + 7 update/deactivate), broker-state
persistence (3, DB-backed), TraderAgent unit tests (8, against a fake
`LLMProvider`), agent-trades HTTP endpoint (6, DB-backed, incl. 1 covering
duplicate-order detection — D024), TechnicalAnalyst unit tests (7, against
a fake `LLMProvider`), technical-analyst wiring on agent-trades (3,
DB-backed), indicator unit tests (9, pure math), Longbridge history
provider unit tests (5, against a fake candlestick client), history-provider
wiring on agent-trades (4, DB-backed), Portfolio module unit tests (6,
pure average-cost-basis replay math, hand-verified), portfolio HTTP
endpoint (23, DB-backed — 15 pre-D027 covering `GET .../portfolio` + 8
new covering `POST .../portfolio/snapshots` and
`GET .../portfolio/history` — D027).

Phase 28 (D031/D032) adds 18, bringing the backend total to **226**: admin
listing endpoints (12, DB-backed — real rows listed with no password/hash
field, deterministic `offset` paging, `limit=501`/`offset=-1` both 422,
and 403/401 on all three routes) and `GET /auth/session` (6, DB-backed —
real expiry/issued_at bounds, no token material in the response, and 401
for a missing, malformed, expired, or deactivated-user token).

Frontend (`apps/web`, Vitest + React Testing Library): **89 tests**, all
passing. 61 through Phase 28 — 34 pre-existing plus 27 added in Phase 28
across `test/PortfolioHistoryChart.test.tsx`, `test/AdminListings.test.tsx`,
and `test/SessionStatus.test.tsx`; 8 more from Phase 29's broker discovery
(`test/BrokerDiscovery.test.tsx`), bringing the pre-Phase-31 total to 69;
and 20 added in Phase 31 (`test/BacktestPanel.test.tsx`, D037). Between
them they cover success, empty results, real error sentinels, 403, 404,
422, the 401→login redirect, and network failure for every component.

Frontend e2e (`apps/web/e2e`, Playwright + Chromium): **11 specs, all
passing** — a separate suite from the Vitest one above, run by
`npm run test:e2e` and never by `npm test`. It cannot run without a real
backend, a real migrated database and the fixtures `scripts/seed_e2e.py`
writes; see docs/DEVELOPMENT_WORKFLOW.md for the bring-up and
docs/DECISIONS.md D045 for what it does and does not cover. The Vitest
count above is unchanged by Phase 37 (99 as run on this branch): the
phase adds no component test, only the `e2e/**` exclude that keeps the
two suites apart.

A full from-scratch integration verification run 2026-08-31 against
`main` at `c2e0ec4` (Phase 39's login-lockout persistence and the
vitest 2→4 major bump merged) confirmed the true post-merge backend
total is **400 tests, all passing** — exactly Phase 39's own
independently-reported 400. Following the same
D028/D033/D036/D040/D043/D046/D048 precedent: fresh Python 3.13 venv
(3.11/3.12 were unavailable on the host; 3.13 is within the project's
`>=3.11` requirement and all dependencies built clean wheels for it),
`pip install -e ".[dev]"`, `ruff check .` (clean, "All checks passed!"),
`mypy apps` (clean, "Success: no issues found in 77 source files" — 77
vs. D048's 76 reflects Phase 39's new login-lockout code), a
separately-named throwaway Postgres/Redis pair on remapped host ports
55432/56379 under its own compose project name `tradingos-verify39`
(the user's own default-port 5432/6379 dev stack, plus their uvicorn on
8000 and Next.js dev server on 3005, was left completely untouched — no
`docker compose down` was ever run, and the throwaway config lived in
a temporary `.env`-free setup: `DATABASE_URL`/`REDIS_URL`/`JWT_SECRET`
were exported as shell variables for this session only, since
`pydantic-settings` gives shell env vars precedence over the repo's real
`.env` file, so the user's own `.env` was never read from or written to),
`alembic upgrade head` — all twelve migrations (0001 through Phase 39's
`0012_users_login_lockout`) applied cleanly in sequence against a real
Postgres 16 (timescaledb image) database with no manual intervention —
and `pytest tests/ -q` against that same real database: 400 passed, 3
pre-existing warnings (the same two `InsecureKeyLengthWarning`s and one
`StarletteDeprecationWarning` seen in every prior verification pass),
zero failures, zero errors. No cross-phase integration bug was found. A
separate frontend check was also run: `cd apps/web && npm install &&
npm run build && npm test` (Vitest unit tests only, per this pass's
scope — Playwright e2e needs its own real backend and was skipped) —
`npm install` reported 0 vulnerabilities, `npm run build` succeeded with
zero TypeScript errors under the bumped vitest/vite toolchain, Vitest
reported **102 tests across 13 files, all passing**, exactly matching
Phase 39's own report, and a standalone `npm audit` confirmed 0
vulnerabilities, verifying Phase 39's CVE-clearing claim. See
docs/DECISIONS.md D051 for the full verification record.

A second, fully independent full from-scratch integration verification
run 2026-08-31 against `main` at `cd847ea` (Phase 40's CI hardening —
secret-scan fix, JWT test fix, new `apps/web` CI job, D052 — already
merged) re-confirmed the true post-merge total unchanged at **400 tests,
all passing**, and specifically set out to independently verify D052's
two headline self-reported fixes rather than take them on trust.
Following the same D028/D033/D036/D040/D043/D046/D048/D051 precedent:
fresh Python 3.13 venv, `pip install -e ".[dev]"`, `bash
scripts/secret_scan.sh` (clean, exit 0 — the exact newly-fixed CI step,
confirmed working independent of D052's own run), `ruff check .` (clean,
"All checks passed!"), `mypy apps` (clean, "Success: no issues found in
77 source files", unchanged from D051/D052 since Phase 40 touched no
`apps/**` source), a separately-named throwaway Postgres/Redis pair on
remapped host ports 15432/16380 under its own compose project name
`trading-os-verify40` (the user's own default-port 5432/6379 dev stack,
plus their uvicorn on 8000 and Next.js dev server on 3005, was confirmed
running via `docker ps`/`netstat` before and after, and left completely
untouched throughout), `alembic upgrade head` → `alembic downgrade base`
→ `alembic upgrade head` (all twelve migrations, still headed at Phase
39's `0012_users_login_lockout` since Phase 40 added none, cleanly in
both directions against a real Postgres 16 database — independently
reconfirming D052's finding that the round-trip was never actually
broken), and `pytest tests/ -q` with `JWT_SECRET_KEY` exported in-shell
exactly as CI does: 400 passed, 3 pre-existing warnings, zero failures,
zero errors, on the first attempt — no transient
`test_missing_jwt_secret_key_fails_closed` failure this time, which is
exactly what D052's `monkeypatch.delenv`-based fix predicts under the
CI-realistic condition (the key exported for the whole run) that broke
that test before the fix; `tests/test_config.py` was also re-run alone
under the same exported key as an extra check (12 passed). A separate
frontend check was run in an isolated copy of `apps/web` (source only,
`node_modules`/`.next` excluded) rather than in place, because the real
`apps/web/node_modules` is shared with the user's live Next.js dev server
and an in-place `npm ci` hit a live file lock on the first attempt
(`EPERM` unlinking a lightningcss binary) — confirming that server was
still genuinely running, and reason enough to isolate the whole frontend
check: `npm ci` (462 packages, 0 vulnerabilities), `npm run build`
(Next.js 16.3.3 Turbopack, TypeScript clean, all 16 routes generated),
`npm test` (**102 tests across 13 files, all passing**, exactly matching
D051/D052, unchanged). No real cross-phase integration bug was found.
See docs/DECISIONS.md D053 for the full verification record.

A third, fully independent full from-scratch integration verification run
2026-08-31 against `main` at `db36ba8` (Phase 41's readiness-probe split
and hardened Dockerfile, D054, already merged) re-confirmed the true
post-merge total unchanged at **404 tests, all passing**, and specifically
set out to independently rebuild D054's own Docker image and re-verify its
two headline claims — the real readiness probe and the non-root
multi-stage image — rather than take them on trust. Following the same
D028/D033/D036/D040/D043/D046/D048/D051/D053 precedent: fresh Python 3.13
venv, `pip install -e ".[dev]"`, `bash scripts/secret_scan.sh` (clean, exit
0), `ruff check .` (clean, "All checks passed!"), `mypy apps` (clean,
"Success: no issues found in 78 source files", unchanged from D054), a
separately-named throwaway Postgres/Redis pair on remapped host ports
55432/56379 under its own compose project name `verify41` (the user's own
default-port 5432/6379 dev stack, plus their uvicorn on 8000 and Next.js
dev server on 3005, was confirmed running via `docker ps`/`netstat` before
and after, and left completely untouched throughout), `alembic upgrade
head` (all twelve migrations, headed at `0012` since Phase 41 added none,
cleanly against a real Postgres 16 database), and `pytest tests/ -q` with
the throwaway env exported in-shell: 404 passed, the same 3 pre-existing
warnings, zero failures, zero errors. The Dockerfile itself was then
independently rebuilt from scratch — `docker build -f apps/api/Dockerfile
-t tradingos-verify41 .` — succeeding cleanly through both the `builder`
and `runtime` stages. `docker run --rm tradingos-verify41 whoami` →
`appuser`, confirming the non-root claim from a genuinely fresh build, not
D054's own image. `docker run --rm tradingos-verify41 alembic current`
against the isolated Postgres over a shared Docker network → `0012
(head)`, confirming the shipped `migrations/`/`alembic.ini` make the image
independently migration-capable. The image was then run as a real
container against the isolated Postgres: `GET /health` →
`{"status":"ok","trading_mode":"research","live_trading_enabled":false}`
(200), `GET /health/ready` →
`{"status":"ready","checks":{"database":{"status":"ok"}}}` (200); Postgres
made unreachable (bad host port, no restart of the API container) →
`GET /health` unchanged at 200 while `GET /health/ready` →
`{"status":"not_ready","checks":{"database":{"status":"error","reason":"connection_failed","error_type":"ConnectionRefusedError"}}}`
(503) — independently reconfirming D054's liveness-vs-readiness split
behaves exactly as claimed. No real cross-phase integration bug was found.
Cleanup was verified complete: the isolated compose stack, its named
volume, the built `tradingos-verify41` image, the throwaway `.env`, and the
verification venv were all removed, and the user's own
`trading-os-postgres-1`/`trading-os-redis-1` containers plus their
uvicorn (8000) and Next.js (3005) dev servers were confirmed still running
and untouched. See docs/DECISIONS.md D055 for the full verification
record.

- Phase 42: observability and lifecycle — per-request correlation IDs and
  an explicit ordered shutdown (2026-08-31, D056). **(1) Structured logs
  carried no correlation key.** A single trade submission emits its risk
  decision, portfolio decision, and fill on three separate JSON lines, and
  in a production stream interleaved across concurrent requests nothing
  grouped them back into one request — making "what happened to the order
  the user says was rejected at 14:03?" unanswerable from the logs alone.
  The new `RequestIDMiddleware` (`apps/api/app/core/request_id.py`,
  registered outermost in `main.py`) assigns every HTTP request a UUID4,
  binds it under the `request_id` key with
  `structlog.contextvars.bind_contextvars`, and returns it as an
  `X-Request-ID` response header on every response including 401s, 404s,
  and validation errors. **No existing `logger.info(...)` call site
  changed** — `merge_contextvars` was already the first processor in
  `configure_logging()`, so the key is merged into every event dict
  automatically. An inbound `X-Request-ID` is honored verbatim when it
  matches `^[A-Za-z0-9._-]{8,128}$` (so a load balancer or the frontend can
  propagate one ID across a hop); a malformed one is **replaced with a
  fresh UUID4 rather than rejected**, because the value is a correlation
  label — never an auth input — and a misconfigured proxy must not be able
  to fail a real trading request. The `request_id` key was explicitly
  asserted not to collide with the secret-redaction patterns in
  `core/logging.py`, and a credential logged alongside a bound ID is still
  redacted. It is pure-ASGI middleware, not `BaseHTTPMiddleware`, so bind
  and unbind stay in one context; cleanup unbinds rather than leaving the
  key set, since uvicorn reuses a task context across requests.
  **(2) The async SQLAlchemy engine was never explicitly disposed.** The
  lifespan stopped only the snapshot scheduler; the process-wide asyncpg
  pool was left for process exit — the OS cleaning up after us rather than
  the service shutting down. Shutdown is now explicit and ordered:
  background work is stopped first (`PortfolioSnapshotScheduler.stop()`
  cancels **and awaits** its task, traced and separately tested, so no
  cycle can still hold a pooled session), then Phase 41/D054's
  `get_engine()` is disposed — never the reverse, which would tear the pool
  out from under a running cycle — followed by a
  `trading_os_shutdown_complete` line. **No new external dependency**:
  structlog's contextvars (installed 26.1.0, API verified against the
  installed package), Starlette's own ASGI protocol, and stdlib `uuid`.
  **424 backend tests passing** over a measured 404 baseline (20 new),
  `ruff check .` and `mypy apps` clean (79 source files). Live-verified
  against an isolated docker compose stack on non-default ports (Postgres
  55432, Redis 56379, the real multi-stage API image on 58000; the user's
  own 5432/6379/8000/3005 dev stack untouched): two `GET /health` calls
  returned two different `x-request-id` headers
  (`683e9914-…` then `220c42b6-…`); a supplied
  `X-Request-ID: lb-edge-phase42-abc123` came back unchanged; a malformed
  `X-Request-ID: bad` was replaced with `23d09c56-…` and still returned
  200. Real container logs for one `GET /health/ready` showed two lines
  from two different modules (`request_id_header_rejected` from the
  middleware and `readiness_check_failed` from the health route) both
  carrying `"request_id": "6d1f0a5e-596a-4d49-8662-dcfc493705a8"`, matching
  that response's header. A real SIGTERM (`docker stop`) produced
  `Waiting for application shutdown.` → `trading_os_shutdown_complete` →
  `Application shutdown complete.` with a clean exit inside the grace
  period. Cleanup was verified complete: the compose stack was torn down
  with `-v`, the built image removed, and the throwaway compose file and
  verification venv deleted; no `.env` was created at any point. See
  docs/DECISIONS.md D056.

- Phase 43: the live-trading execution path — built, gated, and left INERT
  (2026-09-01, D058). **No real order was ever placed, no real broker was
  ever contacted, and `LIVE_TRADING_ENABLED` was never set to `true` — not
  in a default, not in a test fixture, not transiently, not in the smoke
  test. This phase delivers the CODE PATH only.** On a default checkout the
  system is exactly as inert as before it: a request against a live-kind
  broker is refused and no trade context is ever constructed.
  **(1) `LiveBrokerAdapter`** (`apps/api/app/execution/live_broker.py`) —
  the first concrete `BrokerAdapter` that can move real money, implementing
  the same Protocol as `PaperBrokerAdapter` so the OMS and every gate above
  it cannot tell the two apart. Uses the SDK's SYNCHRONOUS
  `longport.openapi.TradeContext` (verified by direct introspection of the
  installed package, not from docs: `TradeContext` has no `.create()`, and
  `submit_order` returns only an `order_id`, never fill information). It
  therefore reads the order back once via `order_detail()` and builds its
  `Fill` from the broker's own `executed_quantity`/`executed_price`; an
  accepted-but-unexecuted order raises `LiveOrderNotFilledError` and the
  route answers `502 LIVE_ORDER_UNCONFIRMED` naming the real order id
  rather than inventing a fill at the caller's estimated price. The
  account snapshot is fetched once per adapter instance (one adapter per
  request), so every gate in a trade reasons about the same book.
  `build_live_broker_adapter()` returns `None` unless `TRADING_MODE=live`
  AND `LIVE_TRADING_ENABLED=true` AND all three `LONGPORT_LIVE_*`
  credentials are set together — separate settings from the read-only
  `LONGPORT_*` quote credentials, so a quote key can never become a
  trading key.
  **(2) Separate, tighter live risk limits** — `LIVE_RISK_*` at 5% max
  position / 20% max exposure / 1% risk per trade, against paper's
  10%/50%/1%, built by the new pure `build_risk_limits(settings, live=)`
  in `apps/api/app/risk/limits.py`. The live branch reads only
  `live_risk_*` and the paper branch only `risk_*`, so paper behaviour is
  structurally unaffected rather than unaffected by convention.
  **(3) A mandatory per-trade `confirm: true`** on `POST
  /brokers/{id}/trades` for a live broker (400 `LIVE_CONFIRMATION_REQUIRED`
  otherwise), plus `trade:submit:live` required IN ADDITION to
  `trade:submit:paper`. A confirmation that lives only in a frontend
  dialog is not one the server can enforce — this is a payload field, and a
  structural safeguard rather than a UX nicety. The agent route stays
  paper-only.
  **(4) The shared gates verified, not assumed** — the emergency stop
  (D039), the duplicate-order check (D024), the Risk Engine, and the
  Portfolio Manager (D029) gate a live order via literally the same
  `_execute_trade()` a paper order runs, and each has its own explicit
  live test against a fake broker double.
  **(5) The per-broker paper/live toggle** — `POST /admin/brokers` and
  `PATCH /admin/brokers/{id}/mode` (both `admin:manage`). A broker row's
  `kind` IS the trading-mode switch: one trade endpoint, and the broker
  row — never a request-body field — decides paper vs live routing.
  Designating a broker live needs `confirm_live: true`; a broker that has
  recorded orders or holds a simulated book can no longer be flipped (409),
  because `orders`/`fills` are append-only with no per-order kind and
  flipping would make simulated and real history indistinguishable.
  No frontend was built (sibling phase 45 owns frontend; a live-trading UI
  deserves its own reviewed phase). Also not built: live limit orders,
  reconciliation of an unfilled live order, fractional shares (refused, not
  rounded), multi-currency live accounts.
  **487 tests passing** (424 pre-existing + 63 new: 23 live-broker unit, 6
  risk-limits unit, 17 live-trade integration, 17 broker-mode-toggle
  integration), ruff + mypy clean across 81 source files. Verified against
  a real Postgres on a remapped port and a real `uvicorn` in
  `TRADING_MODE=paper`; the paper path returned results identical to before
  the phase, and a CONFIRMED live trade still returned `400 NOT_CONFIGURED`
  because `LIVE_TRADING_ENABLED` is false.


A fourth, fully independent full from-scratch integration verification run
2026-08-31 against `main` (Phase 42's request-ID middleware and ordered
shutdown, D056, already merged) re-confirmed the true post-merge total
unchanged at **424 tests, all passing**, and specifically set out to
independently exercise both of D056's headline behaviors live rather than
take them on trust. Following the same
D028/D033/D036/D040/D043/D046/D048/D051/D053/D055 precedent: fresh Python
3.13 venv, `pip install -e ".[dev]"`, `bash scripts/secret_scan.sh` (failed
once — see the real bug below — then clean, exit 0), `ruff check .` (clean,
"All checks passed!"), `mypy apps` (clean, "Success: no issues found in 79
source files", unchanged from D056), a separately-named throwaway
Postgres/Redis pair on remapped host ports 55432/56379 under its own
compose project name `trading-os-verify` (the user's own default-port
5432/6379 dev stack, plus their uvicorn on 8000 and Next.js dev server on
3005, was confirmed running via `docker ps`/`netstat` before and after, and
left completely untouched throughout), `alembic upgrade head` (all twelve
migrations, headed at `0012` since Phase 42 added none, cleanly against a
real Postgres 16 database), and `pytest tests/ -q` with the throwaway env
exported in-shell: 424 passed, the same 3 pre-existing warnings, zero
failures, zero errors. **One real cross-phase bug was found and fixed**:
Phase 42's own `tests/core/test_request_id.py` added a deliberate
secret-look-alike fixture (`api_key="sk-live-should-not-appear"`) without
the `pragma: allowlist secret` marker the D052 secret-scan convention
requires, breaking `scripts/secret_scan.sh`; fixed by naming the literal and
appending the marker on the same matched line. Live request-ID behavior was
then verified against the real multi-stage API image run as a container on
port 58000 (a bare `uvicorn` process was tried first, but Windows has no way
to deliver a true, handler-invoking SIGTERM cross-process, so the container
path was used for a genuine signal test): two plain `GET /health` calls
returned two different `x-request-id` headers; a supplied
`X-Request-ID: my-test-id-12345` came back unchanged; a malformed
`X-Request-ID: a` was replaced with a fresh UUID4 and the request still
returned 200, exactly matching the documented behavior in `docs/API.md`; the
container's stdout showed the matching `request_id_header_rejected` warning
carrying the same `request_id` as the response header, with only
`supplied_length` logged, never the offending value. A real SIGTERM
(`docker stop`) on the running container produced, in order, `Shutting
down` → `Waiting for application shutdown.` → `trading_os_shutdown_complete`
→ `Application shutdown complete.` → `Finished server process [1]`, exit
code 0, no unhandled exception. Cleanup was verified complete: the isolated
compose stack, its volume, the built image, the throwaway `.env`, and the
verification venv were all removed, and the user's own
`trading-os-postgres-1`/`trading-os-redis-1` containers plus their uvicorn
(8000) and Next.js (3005) dev servers were confirmed still running and
untouched. See docs/DECISIONS.md D057 for the full verification record.

- Phase 45: dashboard redesign — a real layout shell and a design-token
  system (2026-09-01, D060). A **visual and layout pass only**: no
  endpoint, no fetch, and no condition governing whether something
  renders was changed. `/dashboard` was a single `max-w-2xl` column of
  eight identically-weighted bordered boxes with ad-hoc
  `border-neutral-*` classes repeated at every call site; it is now a
  12-column grid inside a persistent app shell (left rail carrying brand,
  navigation, live `SessionStatus` and sign-out; sticky header carrying
  the page title and the real `/health` status strip). Panel order stays
  semantically load-bearing: broker discovery ahead of everything
  broker-scoped because it is the source of the `broker_id` those panels
  need (D034), portfolio state and the equity curve leading in the wide
  column, the two order-entry panels sharing a row so neither reads as
  the default, and the backtest last and separate because it alone is not
  broker-scoped (D025). `app/globals.css` gained a semantic token set
  (four-step surface scale, two line weights, three text weights, accent,
  `--pos`/`--neg`/`--grid`) wired through Tailwind v4's `@theme inline`;
  dark is primary (`#060b16`, not `#000000`) and light is a separately
  contrast-checked palette, not an inversion — every text token clears
  4.5:1 against `--surface` in both. `/login` and `/admin` were given the
  same tokens and shell. The Risk Engine's amber and the Portfolio
  Manager's violet deliberately stay explicit palette classes rather than
  tokens, because those hues carry meaning fixed by D029.
  `components/ui/ChartFrame.tsx` adds gridlines, an area fill and axis
  labels to both equity charts but draws no data — `buildPoints` and
  `buildBacktestPoints` are untouched and still hand-rolled, and both
  charts keep their full data table as the accessible fallback. **No new
  dependency**: `package.json` is byte-identical, fonts come from
  `next/font/google`, and the `ui-ux-pro-max` skill's component-library
  suggestion was translated into local Tailwind components instead of
  installed. **102 frontend tests over 13 files pass**, the same 102 as
  before the redesign, with none deleted, skipped or weakened; the two
  that initially failed were fixed by rewording new chrome that had
  named the Portfolio Manager when the backend had not, not by relaxing
  the assertion. Live-verified in a real browser against an isolated
  stack (Postgres 5445, Redis 6445, uvicorn 8045, Next 3045) seeded with
  real users, brokers, grants, a real trade and five real snapshots:
  three distinct `NOT_CONFIGURED:` sentinels (market data, LLM provider,
  history provider), two real 403s (ungranted broker, and
  `admin:manage`), the backend's own login 401, and the full D029 case —
  `approved: true` with `status: "rejected"` and
  `portfolio_action: "reject"` rendering as a neutral Risk Engine block
  beside a violet Portfolio Manager panel. Both colour schemes and a
  1024px viewport were checked. Stack, venv and throwaway compose file
  were removed afterwards; no `.env` was ever created, and the user's own
  5432/6379/8000/3005 services were left untouched.

- Phase 47: broker paper/live mode UI — the frontend D058 deferred
  (2026-09-02, D062). **Frontend-only**: no backend route, schema or
  business rule was changed. D058 built and tested `POST /admin/brokers`
  and `PATCH /admin/brokers/{broker_id}/mode` with no UI at all, on the
  stated grounds that "a live-trading UI deserves its own reviewed
  phase"; until now the one row-level switch deciding which adapter a real
  order reaches could only be flipped by a hand-rolled HTTP call. New:
  `components/admin/BrokerModeAdmin.tsx` (a create-broker form and a
  change-mode form), two pass-through proxies under
  `app/api/admin/brokers/`, a `BrokersList` added to the existing
  `AdminListings.tsx`, and a "Brokers" section on `/admin`. The
  confirmation gate is the point of the phase: `confirm_live` is sent
  **only** when a dedicated, separately-labelled checkbox is ticked **and**
  the target kind is `live` — never pre-checked, omitted entirely (not
  sent as `false`) for a paper target, never injected by either proxy, and
  cleared whenever the target kind changes so a stale tick cannot survive
  a live → paper → live round trip. Submitting unticked is deliberately
  **not** blocked client-side: the backend's own 400
  `LIVE_KIND_CONFIRMATION_REQUIRED` is rendered verbatim, because a
  confirmation living only in the browser is not one the server can
  enforce and a disabled button would have made that real refusal
  unreachable through the product. The 409 for a broker with recorded
  orders is rendered with its full text including its remedy sentence.
  The broker list reads D034's `GET /brokers`, which is **grant-scoped to
  the caller, not platform-wide** — stated in the panel, since an admin
  without a grant will not see a broker there.
  **Two gaps found and deliberately NOT papered over.** (1)
  `AgentTradeResponse` exposes no per-analyst field whatsoever — D059's
  Technical/Fundamental/News analysts run server-side and feed the
  TraderAgent's prompt, but nothing about them is returned, so the
  response cannot even report whether any ran. `AgentTradeForm` now says
  so next to the real result rather than rendering three panels built from
  nothing; a real breakdown needs a backend response-shape change.
  (2) **There is no order/fill list endpoint** — `orders`/`fills` have
  been persisted since Phase 4, but no `GET /brokers/{id}/orders` or
  `/trades` exists in `apps/api/app/api/routes/` or `docs/API.md`, so the
  intended trade-history panel was not built. The platform keeps an
  append-only audit trail no user can read back; this is the largest
  remaining product gap and needs a backend phase.
  **116 frontend tests over 14 files pass**, up from the 102/13 baseline
  (re-confirmed by running the suite before any edit); 14 added, none
  deleted, skipped or weakened. `npm run build` exits 0 with both new
  handlers registered; `npm run lint` ends at the identical pre-existing
  3 problems (2 errors, 1 warning), all in untouched files. Live-verified
  in a real browser against an isolated stack (Postgres 55447, Redis
  63447, uvicorn 8047, Next 3047) seeded via `scripts/seed_e2e.py` plus
  one real paper trade, so a broker genuinely held a recorded order: seven
  scenarios covering both real refusals (400 and 409), a real successful
  paper → live flip on a clean broker, and the stale-tick guard. Both
  colour schemes checked. `LIVE_TRADING_ENABLED` was never touched, no
  live credential was ever set, `/health` reported
  `live_trading_enabled: false` throughout, and the only broker ever
  designated `live` was a throwaway row in a throwaway database. Stack,
  venv and containers removed afterwards; no `.env` created; the sibling
  phase's `tos-p46-pg` and the user's own containers left untouched.
- Phase 50: watchlists — the research half of the research-to-trade
  dashboard (2026-09-03, D067). Before this phase the trade side of that
  loop was complete (D058/D062) while the research side was a single
  symbol input box, so following a handful of names and looking at them
  together had no representation in the product.
  **(1) Schema** — migration `0015` adds `watchlists` (`id`, `user_id`
  FK ON DELETE CASCADE, `name`, `created_at`, plus
  `ix_watchlists_user_created`) and `watchlist_items` (`id`,
  `watchlist_id` FK ON DELETE CASCADE, `symbol`, `added_at`, plus
  `uq_watchlist_item_watchlist_symbol`). Two tables rather than a
  `symbols text[]` column so the one-symbol-per-list invariant is a
  database constraint two concurrent adds cannot both pass, not an
  application check.
  **(2) Five endpoints** — `POST /watchlists`, `GET /watchlists`,
  `DELETE /watchlists/{id}`, `POST /watchlists/{id}/items`,
  `DELETE /watchlists/{id}/items/{symbol}`, plus the payoff,
  `GET /watchlists/{id}/quotes`. These are the **first user-scoped**
  id-addressed resources in the codebase: authentication only, no
  `Permission`, and deliberately **no `BrokerGrant`** — a watchlist
  authorizes nothing and a user with no broker access must still be able
  to research. Ownership lives in one helper all four id-addressed routes
  call; "not there" is 404 and "not yours" is 403, the same order and
  codes `require_broker_access` uses. Watchlists are created explicitly —
  there is no lazily-created default, because a GET must not write a row.
  Symbols are trimmed and upper-cased on the way in and on the DELETE path
  segment, so `"aapl.us"` and `"AAPL.US"` are one entry and a duplicate is
  a real 409.
  **(3) One shared quote-resolution path** — the NOT_CONFIGURED /
  NO_DATA_AVAILABLE decision moved out of `routes/marketdata.py` into
  `apps/api/app/marketdata/resolution.py`, which returns a `ResolvedQuote`
  value instead of raising. `GET /market-data/{symbol}/quote` maps it to
  its unchanged 503/404, and the watchlist route maps it to a per-row
  sentinel — one function, two callers, so the two surfaces cannot drift
  apart about what "no price" means.
  **(4) No fabrication, structurally** — a `WatchlistQuote` carries either
  a price block copied off a real `MarketSnapshot` or a
  `DATA_UNAVAILABLE:`-prefixed string, never neither and never both, and
  the response is always exactly as long as the watchlist. An unpriceable
  symbol keeps its row; with no vendor wired at all the endpoint still
  returns 200, `market_data_configured: false`, and one honest sentinel
  per row (a 503 there would hide the user's own symbols to report
  something the payload already says). Resolution is sequential and a list
  caps at 200 symbols, so one page refresh cannot become a burst of
  rate-limited vendor calls.
  **(5) Frontend** — `apps/web/components/Watchlist.tsx` on the Phase 45 /
  D060 design system, five proxy route handlers under
  `apps/web/app/api/watchlists/`, and a new "Research" section on the
  dashboard holding `Watchlist` and `QuoteLookup` together. Per D034's
  ordering convention neither is broker-scoped, so both sit outside the
  broker-scoped block — `QuoteLookup` moved out of the "Position &
  performance" grid, where it had only ever lived for width.
  Verified on an isolated throwaway stack (Postgres `55432`, Redis
  `56379`, API `18050`/`18051` — never the dev stack's ports): migration
  `downgrade base` → `upgrade head` round-trip clean; backend **615 → 635**
  passing (20 new), frontend **140 → 152** passing (12 new, 17/17 files,
  nothing weakened or deleted); `ruff check .`, `mypy apps` (96 files),
  `npm run build` and `bash scripts/secret_scan.sh` all clean. Live-checked
  over real HTTP: create / add / duplicate-409 / list / quotes /
  cross-user-403 / remove-204 / 404s / delete-204 / 401, with quotes
  exercised both on the committed default (no vendor credentials — every
  row `DATA_UNAVAILABLE: NOT_CONFIGURED: …`, not one invented price) and
  against a stub vendor (two real prices and one
  `DATA_UNAVAILABLE: NO_DATA_AVAILABLE: …` row in the same response).
  `LIVE_TRADING_ENABLED`, the Risk Engine, the Portfolio Manager and the
  emergency stop were untouched; the branch diff contains nothing under
  `apps/api/app/execution/` and does not touch
  `apps/api/app/api/routes/trades.py`, both of which the concurrent
  Phase 48/49 worktrees were editing.
- Phase 46: self-service password reset, with email as an optional vendor
  (2026-09-02, D063). Closes the gap D049 opened: before this phase the
  only recovery path for a forgotten password was a direct SQL update, and
  a user who mistyped five times was locked out for fifteen minutes with
  "no unlock endpoint and no email flow".
  **(1) `password_reset_tokens`** (migration `0013`, the first new table
  since `0012`) — `id`, `user_id` (FK, ON DELETE CASCADE), `token_hash`
  (UNIQUE), `created_at`, `expires_at`, `used_at`. Only a **SHA-256 hex
  digest** of the token is stored, never the token, so a database dump is
  not a set of working reset links. SHA-256 rather than bcrypt is
  deliberate and argued in D063: the input is 32 bytes from
  `secrets.token_urlsafe`, so bcrypt's cost factor buys nothing against a
  256-bit space while making redemption unindexable.
  **(2) `POST /auth/password-reset/request`** (public) — **always 200 with
  one byte-identical body**, across all five branches (address unknown,
  account inactive, per-account throttle tripped, delivery NOT_CONFIGURED,
  send failed). No row is written for an unknown or inactive address, so
  storage cannot be an oracle either. The acknowledgement says a link "has
  been *issued*", never "sent": that is the only wording true in all five
  cases, since three of them send nothing.
  **(3) `POST /auth/password-reset/confirm`** (public) — one 400 sentinel,
  `INVALID_OR_EXPIRED_TOKEN`, for unknown/expired/used/deactivated alike;
  never a stack trace and never a hint at which check failed. On success it
  rehashes with the same bcrypt scheme Phase 7 uses, **clears any active
  D049 login lockout**, and marks every other outstanding unused token for
  that user consumed — all in one transaction. Issuing a second token does
  NOT kill the first (a user who clicked twice cannot tell which email they
  are looking at); invalidation happens at redemption, when the account is
  provably back under someone's control.
  **(4) Email as a NOT_CONFIGURED vendor** — `EMAIL_PROVIDER_BASE_URL` /
  `_API_KEY` / `_FROM_ADDRESS`, all-or-nothing, exactly like `LONGPORT_*`
  (D008/D015) and `LLM_PROVIDER_*` (D018). `notifications/provider.py` is
  the Protocol; `notifications/transactional_email.py` is one
  vendor-agnostic adapter for a generic `POST {base}/emails` bearer-auth
  JSON API (Resend's shape). **Unset is a working state, not a broken
  one**: tokens are still issued, and an admin reads the real link out of
  the new **`POST /admin/users/{id}/password-reset`** (`admin:manage`),
  which answers `"delivery": "NOT_CONFIGURED_returned_directly"` with the
  live `reset_link`. Configured, the same endpoint sends the mail and
  returns `"delivery": "SENT"` with `reset_link: null`. A refused send is a
  real **502** that first invalidates the token it just issued — there is
  no third `delivery` value meaning "we tried and failed", and nothing
  anywhere claims delivery (only that the vendor *accepted* the message).
  **(5) Two throttle layers, honestly scoped** — per-account (default 5/h,
  counted from the table itself so it holds across workers, and never
  visible in the response) and per-IP (default 20/h, real 429, but an
  in-process counter that resets on restart and ignores `X-Forwarded-For`;
  its own docstring says a real rate limit belongs at the reverse proxy).
  **(6) Frontend** — `/forgot-password` and `/reset-password?token=...`,
  plus a "Forgot password?" link always present on `/login` and a
  full-width "Issue password reset link" panel on `/admin`. Both new pages
  use `components/auth/AuthCard.tsx`, which lifts `/login`'s own Phase 45
  layout (D060) verbatim rather than `AppShell` — that shell polls
  `GET /auth/session` and would bounce a signed-out user to `/login` from
  the one page that exists to break that loop. The pages render the
  backend's words verbatim and are tested for it: no "check your inbox",
  and no invented reason behind `INVALID_OR_EXPIRED_TOKEN`.
  Deliberately NOT built: an admin endpoint that sets a password directly
  (an admin who could type it would know it), a session issued on
  successful reset, password rules beyond the existing 8-character floor,
  email verification, self-service registration, HTML email, delivery
  receipts, retries, and a per-row reset button on the admin users listing.
  **615 backend tests passing** (544 baseline re-measured on this branch +
  71 new: 11 token-arithmetic, 8 throttle, 16 email-adapter, 5 config, 22
  public-flow integration, 9 admin-endpoint integration), ruff + mypy
  clean across 93 source files, secret scan clean. **126 frontend tests
  over 15 files** (102/13 baseline + 24 new, none deleted or weakened) and
  `npm run build` clean with all four new routes in the manifest. Verified
  against a real Postgres 16 on remapped port 55446, `alembic upgrade head`
  through `0013` plus the `downgrade base` round-trip. Also **live-verified
  over real HTTP** across three uvicorn instances (8046 NOT_CONFIGURED,
  8047 against a local stub vendor, 8048 against a dead port): registered
  and unknown addresses returned byte-identical 200s and the unknown flood
  wrote zero rows; the admin endpoint gave a real link to an admin and a
  real 403 to a non-admin; the link redeemed once, the old password stopped
  working, and a replay and a fabricated token returned identical
  `INVALID_OR_EXPIRED_TOKEN` bodies; redeeming a newer link killed an older
  one; the per-IP 429 fired for a *registered* address too; the stub vendor
  received exactly the documented `POST /emails` contract and the emailed
  link redeemed; an unreachable vendor produced a real 502 on the admin
  path and an unchanged generic 200 on the public one. Postgres showed only
  64-character hashes, and grepping every server's stdout for the raw
  tokens returned zero matches. The container and its volume were removed
  afterwards and no `.env` was ever created. **No real email vendor was
  contacted and no email credential exists in this branch.**
  `LIVE_TRADING_ENABLED` was never touched and nothing under
  `apps/api/app/execution/` was modified.

- Phase 48: read-only order/fill history (2026-09-03, D065). Closes the
  first of the two gaps Phase 47 (D062) recorded — "the platform keeps an
  append-only audit trail that no user can read back", the one D062 called
  the largest remaining product gap. `orders` and `fills` have been
  written on every trade since Phase 4 (D006) and, until this phase,
  readable only in `psql`: every Risk Engine block reason, every Portfolio
  Manager verdict, every `submitted_by_user_id` and every real execution.
  **Backend-only and read-only.** Three new endpoints —
  `GET /brokers/{broker_id}/orders` (paginated, newest first),
  `GET /brokers/{broker_id}/orders/{order_id}` (one order plus its fills),
  and `GET /brokers/{broker_id}/fills` (a flat blotter, one row per actual
  execution) — in a new `apps/api/app/api/routes/orders.py` with
  `apps/api/app/api/schemas_orders.py`, registered in `main.py` next to the
  portfolio router. Nothing opens a session for writing, constructs a
  broker adapter, or touches the Risk Engine, the Portfolio Manager, the
  emergency stop or `LIVE_TRADING_ENABLED`; the diff contains no change
  under `apps/api/app/execution/`, `apps/api/app/oms/`,
  `apps/api/app/risk/`, `apps/api/app/portfolio_manager/` or `apps/web/`,
  and no migration.
  **Authorization is the existing convention, not a new one**:
  `require_broker_access(Permission.VIEW_PORTFOLIO)` — literally the
  dependency every other broker-scoped read uses. No new `Permission`
  member was added; order history is the history behind exactly the
  positions and P&L `portfolio:view` already authorizes. 404 for an
  unknown broker, 403 for one the caller holds no `BrokerGrant` for,
  unchanged. The detail route additionally filters on `broker_id`, so
  another broker's *real* order id returns 404 rather than confirming it
  exists.
  **Two things deliberately not invented.** (1) There is **no
  `order_type` field**, because there is no such column — `orders` holds
  `side`/`quantity`/`estimated_price` and an optional `stop_price`, and
  emitting a constant `"market"` would read as a recorded fact per row.
  (2) **`broker_kind` is a real join** from `brokers.kind` (the `Broker`
  row `require_broker_access` already loaded), never an inference about
  which adapter ran, because `orders` has no paper/live column — so a
  future frontend can render PAPER/LIVE honestly per row. Both omissions
  are stated in `docs/API.md` and in the schema module's docstring.
  Rejected orders are **included** in the orders listing (they are the
  part of the trail that shows the controls working); the fills route is a
  different grain, not the same list filtered. Pagination is
  `limit` (default 50, max 500) + `offset` in the `{items, limit, offset}`
  envelope D027/D031/D034 use and D062's gap note asked for by name; 501,
  0 and a negative offset are 422s, never silently clamped.
  **636 backend tests passing, up from a 615-test baseline re-measured on
  this branch before any edit** (that baseline run had 614 passing and one
  pre-existing order-dependent flake in
  `tests/api/test_broker_mode_toggle.py`, which passes in isolation and
  passed in the post-change full run — it is unrelated to this phase,
  which writes nothing). 21 new tests in `tests/api/test_order_history.py`,
  none deleted, skipped or weakened. Every order they read back was
  created by a real POST through the real trade path rather than a fixture
  INSERT; the one exception is the live-kind labelling test, which inserts
  its row directly because submitting a live trade would require an
  actually-configured live execution path (D058) and nothing here may
  enable one. `ruff check .` clean and `bash scripts/secret_scan.sh`
  clean; `mypy apps` reports exactly one error, and it is in the
  concurrently-developed `routes/watchlists.py` belonging to a sibling
  phase, not in any file this phase created or modified.
  **Live-verified over real HTTP**, not only through the test client: a
  real `uvicorn` on 127.0.0.1:8148 against an isolated Postgres on
  remapped port 55448 and Redis on 63448, seeded with three real users,
  two real brokers and three real grants. 33 checks, all passing — four
  real filled orders and one genuinely risk-rejected order
  (`exceeds_max_position_size`, from the real engine); the empty-state
  200s; newest-first ordering with the rejection included; the
  two-page reassembly and `limit=501` → 422 / `limit=500` → 200; detail
  byte-identical to its listing row; a real order id from broker B
  returning 404 on broker A and 200 on B; 403 on all three routes for a
  user holding no grant on that broker while the same user could still
  read the broker they *do* hold a grant for; 405 on POST/DELETE with the
  four-row trail intact afterwards; and `GET /health` reporting
  `live_trading_enabled: false` throughout. The isolated stack never used
  the user's own 5432/6379/8000/3005 ports, no `.env` was created, no live
  credential exists in this branch, and no external vendor was contacted.
  **Deliberately NOT built**: any write/cancel/amend route (`orders` is
  append-only by design), symbol/date/status filtering, a cross-broker
  "all my orders" listing, an index tuned for this sort order (a real but
  separate migration — see D065), and the frontend trade-history panel,
  which is a follow-up phase against this now-documented contract.

- Phase 51: frontend order/fill history panel (2026-09-07, D068). Closes
  the follow-up Phase 48/D065 named for itself — the audit trail was
  readable with `curl` and a bearer token, which is not the same as
  readable through the product. **Frontend only.**
  `apps/api/app/api/routes/trades.py` and
  `apps/api/app/api/routes/orders.py` were NOT modified, nothing under
  `apps/api/app/execution/`, `risk/`, `oms/` or `portfolio_manager/` was
  touched, no migration was added, and `LIVE_TRADING_ENABLED` was never
  set. The single backend file in the diff is
  `apps/api/app/api/schemas_orders.py`, and the only change to it is a
  corrected docstring (prose, not logic).
  **(1) Three route handlers** —
  `apps/web/app/api/brokers/[brokerId]/orders/route.ts`,
  `.../orders/[orderId]/route.ts` and `.../fills/route.ts` — proxying
  D065's endpoints on the exact convention of
  `app/api/portfolio/[brokerId]/history/route.ts`: awaited `params`, a 401
  before any network call when the httpOnly auth cookie is absent (D020),
  `limit`/`offset` allowlisted rather than forwarded blindly, a real
  `503 DATA_UNAVAILABLE:` when the API is unreachable, and the backend's
  own status and body passed through unchanged. The detail route forwards
  no query params and passes D065's deliberate 404-for-both-cases through
  untouched. `.../fills` is proxied although the panel reads the orders
  listing: it is a different grain, not the orders list with rejections
  filtered out.
  **(2) `apps/web/components/TradeHistory.tsx`** — broker-scoped (D034,
  via `subscribeToBrokerSelection`), on the Phase 45/D060 primitives, with
  a nine-column table: `submitted_at`, symbol, side, quantity,
  `estimated_price`, fill price, status, `broker_kind`,
  `risk_block_reason`. Wired into `app/dashboard/page.tsx` full-width at
  the end of the "Position & performance" band, above "Order entry", so
  the page reads state → history → action.
  **(3) All FOUR `OrderStatus` values are rendered as four things.** Phase
  49/D066 added `submitted_unconfirmed` (the only non-terminal status) and
  `broker_closed_unfilled`, but `docs/API.md` and `schemas_orders.py`'s
  docstring both still claimed `status` "is `filled` or `rejected`" with
  "no pending state". **Both stale claims were corrected in this phase** —
  a panel built from that prose would have shipped a binary that
  mislabels the two most consequential rows. `rejected` (THIS system
  blocked it; it never reached a venue) and `broker_closed_unfilled` (it
  reached a real broker, which ended it unexecuted) are given distinct
  tones and distinct explanatory text and are never collapsed. An
  unrecognised status renders verbatim with no tone and no invented
  meaning.
  **(4) Nothing invented where the response has no answer.** An order with
  `fills: []` shows an em-dash for fill price — never `0.00`, never a
  fallback to `estimated_price` (the evaluated, not executed, price). No
  order-type column (there is none — D065) and no `broker_status` (D065's
  shape does not project it), both recorded as absences rather than
  guessed.
  **(5) Real pagination.** Prev/Next re-request the backend with a new
  `offset` rather than slicing a cached array. No total count is returned,
  so Next is offered only while the current page came back full. The pager
  renders outside the table block: paging past the last order returns a
  real empty page, and controls living with the rows would vanish exactly
  then and strand the reader — a bug found by clicking the real panel in a
  browser, fixed, and covered by a test.
  **168 web tests passing, up from a 152-test baseline measured on this
  branch before any edit** (17 → 18 files; 16 new in
  `apps/web/test/TradeHistory.test.tsx`, none deleted, skipped or
  weakened). `npm run build` clean with all three handlers in the route
  table. ESLint reports 5 problems, every one in a file this phase did not
  touch (`BrokerDiscovery.tsx`, `SessionStatus.tsx`, `Watchlist.tsx`,
  `lib/session.ts`); all files added or modified here lint clean.
  **Live-verified in a real browser** against an isolated throwaway stack
  (Postgres 55453, Redis 63453, API 8153, `next dev` 3153 — never the dev
  stack's 5432/6379/8000/3005), with `/health` reporting
  `live_trading_enabled: false` throughout. The two paper statuses came
  from the REAL trade path: a real filled order (AAPL.US 10 @ 100) and a
  real risk rejection (`exceeds_max_position_size`, "Proposed notional
  90000 exceeds the max single-position notional 10000") from the real
  deterministic engine. The two live-path statuses cannot be produced
  without enabling live trading, so those rows were inserted directly
  against a `kind=live` broker row — the same device Phase 48 used for its
  live-kind labelling test. All four then rendered under their own names
  with the real joined `broker_kind`; unfilled rows showed no fill price;
  a real 403 ("No access grant for broker …") and 404 ("No broker with id
  …") rendered with no rows drawn; the empty page read as a real empty
  result; and Prev/Next walked offsets 0 → 1 → 2 → 1 over real HTTP with
  Prev still reachable one page past the end. Stack torn down afterwards;
  no `.env` created, no live credential in this branch, no external vendor
  contacted.
  **Deliberately NOT built**: any write/cancel/amend control (the panel is
  read-only and `orders` is append-only by design), a fills-grain blotter
  view (the proxy exists; the UI is a follow-up), symbol/date/status
  filtering, a cross-broker "all my orders" view, and any projection of
  `broker_order_id`/`broker_status`/`reconciled_at`, which would be a
  backend response-shape change.

- Phase 52: per-analyst reads on the agent-trade response (2026-09-07,
  D069). Closes the **second** and last of the two gaps Phase 47 (D062)
  recorded — "`AgentTradeResponse` carries no analyst field at all… the
  response can't even say whether any ran". D059's Technical, Fundamental
  and News analysts have run server-side and fed the TraderAgent's prompt
  since Phase 44; their reads were then discarded. They are now returned.
  **Additive and read-only.** `POST /brokers/{broker_id}/agent-trades`
  gains exactly three response fields — `technical_analyst`,
  `fundamental_analyst`, `news_analyst` — backed by a shared
  `AnalystReadOut` (`stance`/`summary`/`confidence`, mirroring
  `TechnicalRead`/`FundamentalRead`/`NewsRead` field for field) plus one
  subclass per analyst carrying only provenance the route already had:
  `indicator_context` (the verbatim real `SMA(20)=…, RSI(14)=…` string from
  D021's deterministic `indicators.py`, reported not recomputed),
  `data_source` + `fundamentals_as_of` (straight off the vendor's
  `CompanyFundamentals`), and `headline_count` (`len(headlines)` on the
  exact list shown to the analyst). No request field, no new error
  response, no migration, and nothing under `apps/api/app/execution/`,
  `apps/api/app/oms/`, `apps/api/app/risk/`, `apps/api/app/portfolio_manager/`
  or `apps/api/app/safety/`. `LIVE_TRADING_ENABLED` untouched.
  **Nullable per analyst, exactly as D062 required.** All three keys are
  always present; each is independently null because each analyst is
  independently optional and independently failure-isolated. A null means
  only that *this* analyst produced no read, and is never upgraded into a
  synthesised neutral stance with a zero confidence (spec §57). It
  deliberately carries no reason code — six real conditions collapse to it
  and the specific one stays in the structured log, where it already was.
  **Behaviour is unchanged, and that was checked rather than assumed.** The
  three `_*_context()` helpers became `_*_read()` returning the structured
  read; `_render_read()` became `_prompt_context()` rendering the same text
  from it. A direct comparison over 24 stance × `Decimal` combinations
  (including trailing-zero cases like `0.70`) confirms the trader-agent
  prompt text is **byte-identical** to the pre-Phase-52 renderer.
  **The documented flaky test is fixed, not re-documented.**
  `tests/api/test_agent_trades.py` now has a `_no_real_analysts` autouse
  fixture pinning all three analysts and all three market-data providers to
  "not configured" for the whole module, so no test there can reach a real
  `LLM_PROVIDER_BASE_URL`. Scoped to the module rather than the one named
  test because the other five had identical exposure and merely weren't
  timing-sensitive enough to have failed yet. The Known Issues entry is
  deleted, not rewritten.
  **Frontend** — `apps/web/components/AgentTradeForm.tsx` renders an
  "Analyst reads" section with one clearly-labelled sub-panel per analyst:
  the real stance/confidence/summary plus that analyst's provenance row
  when a read exists, and an explicit "No read on this request" note when
  the field is null. Three states are distinguished on purpose: an object
  renders in full, `null` renders the honest no-read note, and an **absent**
  key (an older-shaped response, which cannot be spoken for) renders
  nothing at all — the same rule `PortfolioVerdict` already applies to a
  body with no `portfolio_*` fields. The Phase 47 "no per-analyst
  breakdown / needs a backend response-shape change" caveat is removed
  because it is no longer true. `apps/web/app/dashboard/page.tsx` was not
  touched — a sibling worktree was editing it.
  **705 backend tests passing, up from a 698-test baseline measured on
  this branch before any edit** — and that baseline was 697 passed **and
  one failed**, the documented flake itself, which reproduced on the first
  run because this machine's `.env` genuinely carries a
  `LLM_PROVIDER_BASE_URL`. The post-change run is 705 passed, **0 failed**:
  7 new tests in `tests/api/test_agent_trades.py` (6 → 13) and the
  pre-existing failure gone. The previously-flaky duplicate-detection test
  was then run standalone **six times, passing all six** (2.1–10.4s each)
  against that same `.env`. Frontend **152 → 154**, 17/17 files, with the
  two obsolete Phase 47 tests replaced by four covering all-three-present,
  null-with-a-sibling-still-populated, older-shaped, and pre-submit.
  `ruff check .`, `mypy apps` (99 source files), `npm run build` and
  `bash scripts/secret_scan.sh` all clean.
  **Live-verified over real HTTP** on a real `uvicorn` at 127.0.0.1:8152
  against a second isolated Postgres/Redis (55433/56380), migrated and
  seeded with `scripts/seed_e2e.py`: with all three analysts configured,
  a real filled trade returned all three reads populated — including a
  genuinely-computed `SMA(20)=105.5, RSI(14)=66.666…` — and with none
  configured, the same trade filled with all three fields present and
  `null`. `live_trading_enabled: false` on both runs; no external vendor
  or LLM endpoint was contacted; both throwaway stacks were removed.
  Test/live stacks used remapped ports throughout, never 5432/6379/8000/3005.
  **Re-verified after merging `main` at `87d6882`** (Phase 51/D068), since
  the counts above were measured against the pre-Phase-51 base `81a18fa`:
  backend still **705 passed, 0 failed** (Phase 51 added no backend test),
  frontend **170 passed / 18 files** (this phase's 154 plus Phase 51's 16),
  with `ruff`, `mypy`, `npm run build` and `secret_scan.sh` all still clean.
  The merge conflicted only in `docs/DECISIONS.md` and this file, only
  because both phases appended an entry at the same anchor, and every
  conflict was resolved additively.
  **Deliberately NOT built**: any reason code or status enum on a null
  analyst field (see D069), structured numeric `sma`/`rsi` fields in place
  of the verbatim `indicator_context` string, a `SentimentAnalyst` (still
  out of scope per D059), and any change to which analysts run or when.

## Known Issues

Open, known-and-scoped limitations (not defects): the snapshot scheduler's
market-hours gate is weekend-only — intraday after-hours and exchange
holidays are not checked (Phase 35/D042).

*(Corrected 2026-09-14: this note previously also claimed "each API worker
process still runs its own independent scheduler loop, so >1 worker would
multiply snapshot rows (D030)." That was fixed by Phase 38/D047, which put
a non-blocking Postgres advisory lock around each cycle — verified present
in `apps/api/app/portfolio/scheduler.py`. The stale line is removed rather
than left to mislead an operator into avoiding a second worker.)*

Both backend gaps identified by Phase 47 (D062) while building the frontend
for them are now **CLOSED**:

1. ~~**No order/fill list endpoint.**~~ **FULLY CLOSED (backend Phase 48,
   D065; frontend Phase 51, D068.)**
   `GET /brokers/{broker_id}/orders`, `.../orders/{order_id}` and
   `.../fills` expose the append-only trail on the D027/D031
   `{items, limit, offset}` convention as that gap note asked for, and
   `apps/web/components/TradeHistory.tsx` now reads it through the
   product. The follow-up D065 named for itself has landed.
   One narrower gap is left behind by it: `OrderResponse` does not project
   `broker_order_id`, `broker_status` or `reconciled_at`, so a client
   cannot show the venue's own wording for a `broker_closed_unfilled`
   order. That is a response-shape change, hence a backend phase.
2. ~~**`AgentTradeResponse` returns no per-analyst output.**~~
   **CLOSED (Phase 52, D069.)** The response now carries
   `technical_analyst`, `fundamental_analyst` and `news_analyst`, each
   holding that analyst's real stance/summary/confidence plus the
   provenance already present in the server's own inputs. Nullable **per
   analyst**, exactly as that gap note required — a null is one analyst's
   real absence and implies nothing about the other two, and is never
   upgraded into a synthesised neutral read. The frontend renders all
   three in `apps/web/components/AgentTradeForm.tsx`, and the Phase 47
   "no analyst context in the response" caveat is gone because it is no
   longer true.
