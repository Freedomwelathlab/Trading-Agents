"""Routes a bar-backfill request to the vendor that can actually serve the
symbol (Phase 71, docs/DECISIONS.md D089).

Two vendors now feed `market_data_bars`, and they cover disjoint universes
rather than overlapping ones:

  * **Longbridge** prices US equities and ETFs (`AAPL.US`, `IBIT.US`). It
    has no spot BTC/USD instrument on this account at all - `BTCUSD.BKKT`
    answers `301600 invalid symbol` - so crypto cannot be asked of it.
  * **Coinbase** prices crypto products (`BTC-USD`, `ETH-USD`) and knows
    nothing about equities.

Because the universes are disjoint, routing is a decision about WHICH
vendor can answer, not a preference ordering with a fallback. That is the
important difference from `MarketDataRouter`, which tries providers in turn
for the same symbol: a fallback here would mean answering "what are AAPL's
bars?" with a crypto exchange's silence, or worse, routing `BTC-USD` to a
vendor that returns an unrelated instrument. So an unroutable symbol is
REFUSED with a message naming both conventions, never attempted against
every provider in the hope one works.

**Routing is on symbol SHAPE, which is a real distinction, not a guess.**
Longbridge symbols always carry a market suffix after a dot (`.US`, `.HK`,
`.SG`); Coinbase product ids are always `BASE-QUOTE` with a hyphen and
never a dot. The two grammars cannot collide, and a symbol matching
neither is exactly the case that should be refused rather than guessed at.
"""

from datetime import date

from apps.api.app.core.logging import get_logger
from apps.api.app.marketdata.bar_provider import Bar, HistoricalBarProvider

logger = get_logger(__name__)


class UnroutableSymbolError(Exception):
    """No configured vendor serves symbols of this shape.

    Deliberately NOT `DataUnavailableError`: that means "the vendor was
    asked and had nothing", which a caller may reasonably treat as a gap to
    backfill later. This means "nothing was asked, because no vendor here
    speaks this symbol" - a configuration or typo problem the operator must
    fix, and one that retrying will never resolve.
    """


class BarBackfillRouter:
    """Dispatches `get_bars` to whichever vendor serves the symbol.

    Implements `HistoricalBarProvider` itself, so every existing caller -
    `run_backfill_job`, and the admin route behind it - takes this in place
    of a single provider with no change at all.
    """

    name = "bar-router"

    def __init__(
        self,
        *,
        equity_provider: HistoricalBarProvider | None = None,
        crypto_provider: HistoricalBarProvider | None = None,
    ) -> None:
        self._equity = equity_provider
        self._crypto = crypto_provider

    @property
    def configured_vendors(self) -> tuple[str, ...]:
        """Which vendors this router can actually reach, for the startup
        banner and the health endpoint. Named rather than counted so an
        operator can see WHICH half is missing - "crypto only" is a very
        different state from "equities only", and both are legitimate."""
        names = []
        if self._equity is not None:
            names.append(getattr(self._equity, "name", "equity"))
        if self._crypto is not None:
            names.append(getattr(self._crypto, "name", "crypto"))
        return tuple(names)

    @staticmethod
    def is_crypto_symbol(symbol: str) -> bool:
        """`BASE-QUOTE` with a hyphen and no dot - a Coinbase product id.

        Both halves of the test matter. Requiring the hyphen keeps `AAPL`
        from routing to crypto; forbidding the dot keeps a hypothetical
        `BRK-B.US` (a Longbridge symbol that happens to contain a hyphen)
        from being mistaken for a crypto product.
        """
        return "-" in symbol and "." not in symbol

    def provider_for(self, symbol: str) -> HistoricalBarProvider:
        """The vendor for this symbol, or a refusal naming why.

        The two failure messages are deliberately different. "Recognized
        the shape, no vendor configured" is an operator fixing credentials;
        "did not recognize the shape" is an operator fixing a typo. One
        generic message would leave them guessing which.
        """
        if self.is_crypto_symbol(symbol):
            if self._crypto is None:
                raise UnroutableSymbolError(
                    f"{symbol!r} looks like a crypto product id, but no crypto bar "
                    "provider is configured."
                )
            return self._crypto

        if "." in symbol:
            if self._equity is None:
                raise UnroutableSymbolError(
                    f"{symbol!r} looks like a Longbridge symbol, but no equity bar "
                    "provider is configured - set LONGPORT_APP_KEY, "
                    "LONGPORT_APP_SECRET and LONGPORT_ACCESS_TOKEN together."
                )
            return self._equity

        raise UnroutableSymbolError(
            f"{symbol!r} matches no known symbol convention. Use a Longbridge symbol "
            "with a market suffix (e.g. 'IBIT.US') or a Coinbase product id "
            "(e.g. 'BTC-USD')."
        )

    async def get_bars(
        self, symbol: str, *, bar_interval: str, start_date: date, end_date: date
    ) -> list[Bar]:
        provider = self.provider_for(symbol)
        logger.info(
            "bar_backfill_routed",
            symbol=symbol,
            bar_interval=bar_interval,
            vendor=getattr(provider, "name", type(provider).__name__),
        )
        return await provider.get_bars(
            symbol, bar_interval=bar_interval, start_date=start_date, end_date=end_date
        )
