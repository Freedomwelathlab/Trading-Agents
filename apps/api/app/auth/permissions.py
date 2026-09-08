"""Known permission strings. A Role's `permissions` column holds a list of
these values - adding a new permission means adding a member here AND a
check that actually enforces it somewhere; the enum alone does nothing.
"""

import enum


class Permission(str, enum.Enum):  # noqa: UP042 (str mixin kept for interop)
    SUBMIT_PAPER_TRADE = "trade:submit:paper"
    SUBMIT_LIVE_TRADE = "trade:submit:live"
    """Reserved, not enforced anywhere yet - there is no live execution
    path (spec Sec3/Sec46) for this permission to gate. Do not wire this to
    the trades endpoint's live-broker branch without also building the
    live confirmation flow spec Sec56 requires; granting this permission
    today would be a permission that does nothing, not a shortcut to live
    trading."""
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
