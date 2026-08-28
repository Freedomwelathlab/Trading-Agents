"""Typed errors for portfolio computation. No bare exceptions - callers
(the HTTP route) map each of these to a specific DATA_UNAVAILABLE-style
response rather than guessing what went wrong (spec's no-fabrication
posture applies to reporting just as much as to trade submission)."""


class MissingMarkError(Exception):
    """Raised when compute_portfolio_snapshot needs a current price for a
    symbol the broker holds a nonzero position in, and the caller did not
    supply one in `marks`. Mirrors PaperBrokerAdapter.get_account_state's
    identical discipline (apps/api/app/execution/paper_broker.py) - a
    missing mark is never filled in with a stale or invented price."""


class BrokerAccountNotFoundError(Exception):
    """Raised when the broker has no broker_accounts row and no
    default_starting_cash was supplied to fall back on. A broker that has
    never had a trade (or a paper broker load) legitimately has no
    persisted cash row yet (see apps/api/app/execution/persistence.py's
    lazy-seed behavior) - this is a real data-availability gap, not a bug,
    but it is still not something this module will silently paper over as
    zero cash unless the caller explicitly opts into that via
    default_starting_cash."""
