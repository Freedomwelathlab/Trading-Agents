"""Known permission strings. A Role's `permissions` column holds a list of
these values - adding a new permission means adding a member here AND a
check that actually enforces it somewhere; the enum alone does nothing.
"""

import enum


class Permission(str, enum.Enum):  # noqa: UP042 (str mixin kept for interop)
    SUBMIT_PAPER_TRADE = "trade:submit:paper"
    SUBMIT_LIVE_TRADE = "trade:submit:live"
    """Phase 43 (D058). STALE DOCSTRING FIXED Phase 68 (D086): this used to
    say "reserved, not enforced anywhere yet" - true before Phase 43, false
    since it. It IS enforced today, in
    `apps/api/app/api/routes/trades.py::_authorize_live_trade`, required IN
    ADDITION to `SUBMIT_PAPER_TRADE` (which `require_broker_access` already
    checked) for a request against a `kind=LIVE` broker - checked LAST of
    that function's three gates (confirmation, then live-path
    configuration, then this permission), by design, because it is the
    only one of the three that needs the resolved role. No ADMIN override:
    an operational admin is not automatically authorized to place a live
    order, for the same reason `STRATEGY_APPROVE_LIVE_DEPLOYMENT` below
    carries none."""
    VIEW_PORTFOLIO = "portfolio:view"
    """Gates GET /brokers/{broker_id}/portfolio (D022). Deliberately
    separate from SUBMIT_PAPER_TRADE - viewing a broker's positions/P&L is
    a strictly weaker capability than moving money in it, and a role that
    should only report on a broker (e.g. a read-only dashboard user)
    should not have to also hold trade-submission rights to do so. Still
    combined with a BrokerGrant for the specific broker (require_broker_access)
    - this permission alone authorizes viewing no broker's portfolio."""
    ADMIN = "admin:manage"
    """Gates every route under /admin - creating users, roles, and broker
    grants. One coarse permission, not per-resource ones (admin:create_user
    etc.) - see docs/DECISIONS.md D013 for why. The very first admin user
    and role still have to be created by direct DB insert; there is no
    user holding this permission to call these routes with the first time
    (bootstrap problem, documented, not solved by this permission alone)."""
    STRATEGY_MANAGE = "strategy:manage"
    """Phase 54. Gates every route under /strategies - create, read, edit,
    fork a version, validate a version, archive - but ONLY over the
    caller's OWN strategies.

    Ownership is the `strategies.owner_user_id` column, checked per route
    (`_load_owned_strategy` in apps/api/app/api/routes/strategies.py), not a
    separate grant table like BrokerGrant (D012). There is exactly one
    owner per strategy, set at creation and never reassigned this phase, so
    a join table would model a many-to-many relationship that does not
    exist. Holding this permission therefore authorizes acting on your own
    strategies and no one else's: every route additionally checks
    `strategy.owner_user_id == current_user.id` and answers 403 when it
    does not hold (404 when no strategy has that id at all, so a
    nonexistent id never 403s merely because nobody can own a row that
    isn't there - the same order and codes `require_broker_access` and
    `_owned_watchlist` already use).

    Deliberately NOT built this phase: an ADMIN override. `admin:manage` is
    an operational permission (users, roles, grants, market-data
    ingestion), and letting it silently read or edit every user's strategy
    research would be a new and much broader capability than any route it
    gates today. If cross-user visibility is ever wanted it should be its
    own permission with its own enforcing check, not a side effect of this
    one.
    """
    STRATEGY_BACKTEST = "strategy:backtest"
    """Phase 55. Gates the whole
    `/strategies/{id}/versions/{id}/backtests` + `/backtest-runs` surface at
    the router level, exactly the way STRATEGY_MANAGE gates `/strategies`.

    Deliberately SEPARATE from STRATEGY_MANAGE rather than folded into it,
    because the two capabilities genuinely come apart in both directions. A
    role could hold this without being able to author or edit strategies -
    an evaluation or research account that may run and read results over
    definitions someone else wrote. And a role could hold STRATEGY_MANAGE
    without this one: running a backtest reads the whole persisted bar
    store and writes rows of its own, where editing a definition is a
    self-contained JSON edit. Merging them would make granting either one
    grant both, with no way back.

    Ownership is still checked per request ON TOP of this permission - a
    caller may only backtest, and only see runs on, their OWN strategies
    (`_load_owned_strategy` / `_load_version`, imported from
    apps/api/app/api/routes/strategies.py rather than re-implemented). That
    is the same two-part authorization shape STRATEGY_MANAGE already
    established (D071), not a new pattern: the permission says this account
    may run backtests at all, the `owner_user_id` check says over which
    strategies. Neither alone is sufficient.

    Like STRATEGY_MANAGE, this deliberately carries no ADMIN override.
    """
    STRATEGY_SIGNAL = "strategy:signal"
    """Phase 61. Gates evaluating a strategy's CURRENT signal
    (`POST /strategies/{id}/versions/{id}/signals`) and the signal listings
    (`GET` on that same path, and `GET /signal-evaluations/{id}`), at the
    router level exactly as STRATEGY_BACKTEST gates the backtest surface.

    Deliberately SEPARATE from STRATEGY_BACKTEST, not folded into it. A
    backtest is a historical what-if - it asks what WOULD have happened over a
    window that is already over, and nothing about its answer is a
    present-tense instruction. A signal is "what should happen now, for this
    symbol, today", and it is the input Phase 63's paper-trading runner will
    act on. A role that may study a strategy's past - an evaluation or
    research account reading someone's else's results - must not automatically
    be able to ask what that strategy says to DO right now, because that answer
    is one step from an order rather than one step from a chart. The converse
    also comes apart: a role could hold this without STRATEGY_BACKTEST, only
    ever reading current signals for definitions it never re-runs history on.
    Merging the two would make granting either grant both, with no way back.

    Ownership is still checked per request ON TOP of this permission - a caller
    may only evaluate, and only see signals on, their OWN strategies
    (`_load_owned_strategy` / `_load_version`, imported from
    apps/api/app/api/routes/strategies.py rather than re-implemented). Same
    two-part authorization shape STRATEGY_MANAGE established (D071): the
    permission says this account may ask for signals at all, the
    `owner_user_id` check says over which strategies. Neither alone suffices.

    Like both permissions above, this deliberately carries no ADMIN override,
    and holding it authorizes no trade of any kind - a signal is a proposal,
    and every actual order still goes through the deterministic Risk Engine
    and the trade-submission permissions.
    """
    STRATEGY_DEPLOY = "strategy:deploy"
    """Phase 63. Gates creating, listing, reading, pausing, resuming and
    stopping a `StrategyDeployment` - a validated version put on the
    scheduled paper-trading runner.

    This is the first permission that can lead to an order being placed with
    no HTTP request behind that specific order, so it is deliberately its
    own capability, separate from every strategy permission above AND from
    `trade:submit:paper`. Holding `strategy:signal` lets a caller ask what a
    strategy says to do; holding this lets them arrange for the system to
    ACT on that answer on a timer. Those are different levels of trust and a
    role should be able to hold the first without the second.

    It does NOT include approval. Moving a deployment from
    `PENDING_APPROVAL` to `ACTIVE` - the point at which the runner will
    start trading it - requires `STRATEGY_APPROVE_DEPLOYMENT` below, a
    deliberately separate permission so an organization can require that the
    person who approves a strategy for trading is not the same person who
    deployed it. The code does not force approver != requester (that is a
    policy choice a deployment-review workflow would layer on), but the
    permission split is what makes that policy expressible.

    Ownership is checked per request on top of this permission
    (`_load_owned_strategy` / `_load_version` over the deployment's version),
    the same two-part shape as every strategy permission since D071. No
    ADMIN override.
    """
    STRATEGY_APPROVE_DEPLOYMENT = "strategy:approve_deployment"
    """Phase 63. Gates ONLY the approval action that moves a
    `StrategyDeployment` from `PENDING_APPROVAL` to `ACTIVE` - the mandatory
    human gate spec §25/§52 and docs/TRADING_SAFETY.md require before a
    strategy trades, even in paper mode.

    Separate from `STRATEGY_DEPLOY` on purpose: approval is the single most
    consequential action in the deployment lifecycle (it is what lets the
    runner place orders), and separating it lets a role deploy-and-propose
    without being able to green-light its own proposal. Ownership of the
    underlying strategy is still checked. No ADMIN override - an operational
    admin is not automatically a strategy-trading approver.

    Approving a `mode='live'` deployment ALSO requires
    `STRATEGY_APPROVE_LIVE_DEPLOYMENT` below - this permission alone is
    enough for paper, never for live.
    """
    STRATEGY_APPROVE_LIVE_DEPLOYMENT = "strategy:approve_live_deployment"
    """Phase 64 (D082). Required IN ADDITION to `STRATEGY_APPROVE_DEPLOYMENT`
    to approve a `mode='live'` deployment - the same "strictly more
    demanding than either alone" shape `SUBMIT_LIVE_TRADE` was meant to add
    on top of `SUBMIT_PAPER_TRADE` for a single live order (spec Sec56).

    Holding this permission does not make the runner place a live order: it
    cannot, in this phase (see `StrategyDeploymentRunStatus.
    SKIPPED_LIVE_TRADING_DISABLED`). What this permission gates today is
    strictly the bookkeeping act of approving a live deployment's existence
    - real live execution needs a future, separately-approved runner change,
    at which point this is the permission that will already be gating the
    one step upstream of it. Granting this today is not itself a step
    toward enabling live trading.
    """
