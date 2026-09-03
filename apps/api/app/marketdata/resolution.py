"""The single quote-resolution path shared by every route that needs a
live price for one symbol (Phase 50).

Before this module existed there was exactly one such route
(`GET /market-data/{symbol}/quote`), so its two failure cases -
"no vendor is configured at all" and "every configured vendor had no data
for this symbol" - lived inline in the route handler. `GET
/watchlists/{id}/quotes` needs the same two distinctions for N symbols,
and a second inline copy of them is precisely how the two surfaces would
eventually disagree about what "no price" means.

So the decision lives here, once, and returns a value instead of raising
an HTTP error: the caller decides how to render it. That matters because
the two callers legitimately render it differently and both are correct:

- The single-quote route turns NOT_CONFIGURED into a 503 and
  NO_DATA_AVAILABLE into a 404, because the whole response is that one
  quote.
- The watchlist route keeps a 200 and puts the sentinel on the individual
  row, because the response is a list and the other rows may well have
  real prices. A row with no price is a row that says so.

What is NOT negotiable in either case, and is the reason this returns a
sentinel rather than an Optional price: there is no branch anywhere below
that produces a number when the vendor did not (spec Sec57, docs/
TRADING_SAFETY.md). `price` is populated from a real `MarketSnapshot` or
it is absent.
"""

from dataclasses import dataclass

from apps.api.app.marketdata.models import MarketSnapshot
from apps.api.app.marketdata.router import MarketDataRouter, NoDataAvailableError

NOT_CONFIGURED_DETAIL = (
    "NOT_CONFIGURED: no market data vendor is wired (see docs/DECISIONS.md D008/D015)."
)
"""The exact string `GET /market-data/{symbol}/quote` has returned since
D015. Kept verbatim so factoring this out changed no wire contract."""

DATA_UNAVAILABLE_PREFIX = "DATA_UNAVAILABLE"
"""Row-level sentinel prefix for surfaces that render per-symbol outcomes
(docs/TRADING_SAFETY.md's "No fabrication" section). The underlying cause -
the `NOT_CONFIGURED:` string above, or the router's own
`NO_DATA_AVAILABLE:` message - is preserved after it rather than replaced,
so a caller can still tell "nothing is wired" apart from "this symbol has
no price"."""


@dataclass(frozen=True)
class ResolvedQuote:
    """Exactly one of `snapshot` / `unavailable` is set.

    `not_configured` distinguishes the two failure modes without asking
    any caller to string-match: True means no vendor exists at all (a
    deployment fact, identical for every symbol), False means this
    particular symbol had no data from the vendors that do exist.
    """

    symbol: str
    snapshot: MarketSnapshot | None
    unavailable: str | None
    not_configured: bool

    @property
    def is_available(self) -> bool:
        return self.snapshot is not None


async def resolve_quote(
    market_data_router: MarketDataRouter | None, symbol: str
) -> ResolvedQuote:
    """Resolve one symbol through the configured vendor chain.

    `market_data_router is None` is the NOT_CONFIGURED case (D008/D015):
    `get_market_data_router` returns None when no vendor credentials are
    set. It is never treated as "try harder" or as an empty successful
    result.
    """
    if market_data_router is None:
        return ResolvedQuote(
            symbol=symbol,
            snapshot=None,
            unavailable=NOT_CONFIGURED_DETAIL,
            not_configured=True,
        )

    try:
        snapshot = await market_data_router.get_snapshot(symbol)
    except NoDataAvailableError as exc:
        # Message is already NO_DATA_AVAILABLE:-prefixed by the router.
        return ResolvedQuote(
            symbol=symbol, snapshot=None, unavailable=str(exc), not_configured=False
        )

    return ResolvedQuote(symbol=symbol, snapshot=snapshot, unavailable=None, not_configured=False)


def row_sentinel(resolved: ResolvedQuote) -> str:
    """The `DATA_UNAVAILABLE: <real cause>` string for a per-row surface.

    Only ever called on an unavailable result; calling it on an available
    one is a programming error, not a state to paper over, so it asserts
    rather than inventing a message.
    """
    if resolved.unavailable is None:
        raise ValueError(
            f"row_sentinel called on an available quote for {resolved.symbol!r} - "
            "there is no unavailability to describe."
        )
    return f"{DATA_UNAVAILABLE_PREFIX}: {resolved.unavailable}"
