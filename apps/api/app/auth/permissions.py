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
