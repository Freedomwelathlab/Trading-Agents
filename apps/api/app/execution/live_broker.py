"""Longbridge LIVE broker adapter - the first concrete `BrokerAdapter`
that can move real money (Phase 43, docs/DECISIONS.md D058).

READ THIS BEFORE TOUCHING ANYTHING HERE.

This module is INERT BY DEFAULT and must stay that way. Nothing in it
ever runs unless ALL of the following are true at once:

  1. `TRADING_MODE=live`
  2. `LIVE_TRADING_ENABLED=true`
  3. all three `LONGPORT_LIVE_*` credentials are set together

`build_live_broker_adapter()` returns `None` - never a fabricated or
degraded adapter - if any one of those is missing, and the trade route
renders that as `NOT_CONFIGURED` (spec Sec57). The repository's committed
defaults have `LIVE_TRADING_ENABLED=false`, so on a default checkout this
module constructs nothing and reaches no network.

Structural parallel to `PaperBrokerAdapter`
-------------------------------------------
Same `BrokerAdapter` Protocol, same synchronous shape, same
"caller supplies the marks, we never invent one" rule. That is the point:
`apps/api/app/oms/service.py` and the risk/portfolio gates above it treat
a live broker exactly like a paper one, so a live trade cannot take a
shortcut around any check a paper trade goes through.

Why the SYNCHRONOUS `longport.openapi.TradeContext`
---------------------------------------------------
`BrokerAdapter` is synchronous because `PaperBrokerAdapter` and
`submit_trade()` are, and the SDK ships both a sync `TradeContext` and an
`AsyncTradeContext` with identical method signatures. Using the sync one
keeps this adapter structurally interchangeable with the paper one
instead of forcing the OMS, the backtest engine, and every existing test
to become async purely to accommodate a second adapter. The cost is real
and deliberate: a live submission briefly blocks the serving event loop.
That is an acceptable trade for one human-confirmed order at a time (a
live order requires `confirm: true` per request - see
apps/api/app/api/routes/trades.py); it would not be acceptable for a
high-rate automated path, and moving the whole order path to async is
the right fix if such a path is ever built.

SDK surface verified by direct introspection of the installed `longport`
package, not from documentation or memory (the D008/D015/D021 discipline):

  - `Config.from_apikey(app_key=, app_secret=, access_token=)`
  - `TradeContext(config)` - constructor, no `.create()` (that exists only
    on `AsyncTradeContext`)
  - `TradeContext.submit_order(symbol, order_type, side, submitted_quantity,
    time_in_force, submitted_price=None, ...)` -> `SubmitOrderResponse`,
    whose ONLY attribute is `order_id`
  - `TradeContext.order_detail(order_id)` -> `OrderDetail` with
    `.status`, `.executed_quantity`, `.executed_price`, `.updated_at`
  - `TradeContext.stock_positions(symbols=None)` ->
    `StockPositionsResponse.channels[].positions[]` with `.symbol`,
    `.quantity`
  - `TradeContext.account_balance(currency=None)` -> a sequence of
    `AccountBalance` with `.currency` and `.total_cash`
  - `OrderSide.Buy/.Sell`, `OrderType.MO`, `TimeInForceType.Day`

No fabricated fills
-------------------
`submit_order()` on a real broker returns an order id, NOT a fill - a
market order is not guaranteed to be executed the instant it is accepted.
This adapter therefore reads the order back once and builds its `Fill`
from the broker's OWN `executed_quantity`/`executed_price`. If the order
is not (at least partially) executed at that moment, it raises
`LiveOrderNotFilledError` carrying the real order id rather than
inventing a fill at `market_price` - the caller's `market_price` argument
is used ONLY to keep the Protocol signature identical to the paper
adapter's and is never written into a `Fill`. There is deliberately no
retry/poll loop: silently re-reading a live order in a loop hides a real,
money-moving order behind a request timeout, and reconciling an accepted
but unfilled live order is its own (not yet built) problem.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from apps.api.app.core.config import Settings, TradingMode
from apps.api.app.execution.broker import (
    Fill,
    OrderNotConfirmedError,
    OrderRequest,
)
from apps.api.app.risk.models import AccountState, Side


class LiveBrokerError(Exception):
    """Any failure of the live broker connection itself. Never swallowed
    into a 'probably fine' result - docs/TRADING_SAFETY.md's fail-closed
    rule applies hardest here."""


class LiveOrderNotFilledError(LiveBrokerError, OrderNotConfirmedError):
    """The broker accepted the order but has not (yet) executed any of it.
    Carries the real `order_id` so the position can be reconciled; this is
    NOT a rejection and the order may still execute.

    Phase 49 (D066) added the `OrderNotConfirmedError` base so the OMS can
    attach the decision context that produced the order without importing
    this live-specific module. `LiveBrokerError` is kept FIRST in the bases
    so the existing `except LiveBrokerError` arms in
    apps/api/app/api/routes/trades.py keep catching this exactly as before;
    nothing about the Phase 43 behaviour changes."""

    def __init__(self, message: str, *, order_id: str) -> None:
        OrderNotConfirmedError.__init__(self, message, order_id=order_id)


class LiveTradeClient(Protocol):
    """The minimal slice of `longport.openapi.TradeContext` this adapter
    actually depends on, expressed as our own Protocol so tests inject a
    fake without real credentials and without a network call - exactly the
    pattern `LongbridgeQuoteClient` established in D015. The real SDK is
    imported only by `build_live_broker_adapter()` below, never by this
    class or by any test."""

    def submit_order(
        self,
        symbol: str,
        order_type: Any,
        side: Any,
        submitted_quantity: Decimal,
        time_in_force: Any,
        submitted_price: Decimal | None = None,
    ) -> Any: ...

    def order_detail(self, order_id: str) -> Any: ...

    def cancel_order(self, order_id: str) -> Any: ...

    def stock_positions(self, symbols: list[str] | None = None) -> Any: ...

    def account_balance(self, currency: str | None = None) -> Sequence[Any]: ...


_EXECUTED_STATUSES = frozenset({"Filled", "PartialFilled"})

_TERMINAL_STATUSES = frozenset(
    {"Filled", "Canceled", "Expired", "Rejected", "PartialWithdrawal"}
)
"""Longbridge `OrderStatus` names meaning "this order will not progress any
further" (Phase 49, D066).

Every name here was read off the installed SDK by direct introspection of
`longport.openapi.OrderStatus`, not from documentation or memory (the
D008/D015/D021 discipline). The full member list at the time of writing is
Canceled, Expired, Filled, New, NotReported, PartialFilled,
PartialWithdrawal, PendingCancel, PendingReplace, ProtectedNotReported,
Rejected, Replaced, ReplacedNotReported, Unknown, VarietiesNotReported,
WaitToCancel, WaitToNew, WaitToReplace. Note the SDK's spelling is
`Canceled`, one 'l'.

**Everything not listed here is treated as STILL OPEN**, including
`PartialFilled`, `Unknown`, and any status a future SDK version adds. That
asymmetry is deliberate and is the fail-closed direction: leaving a live
order under observation for another cycle costs one API call, whereas
declaring it finished on a status this code does not actually understand
would permanently record an outcome the broker never reported.

`PartialFilled` in particular is NOT terminal here, even though the
submit-time path above accepts it as a real fill. The two are answering
different questions. At submit time a caller is synchronously waiting and a
partial execution is genuinely the best true answer available. At
reconciliation time there is no one waiting, so the honest move is to keep
watching until the venue itself says the order is done - recording a
half-done order as final would freeze a number that is still moving, in an
append-only audit trail, forever."""


@dataclass(frozen=True)
class LiveOrderStatus:
    """One real, broker-reported answer to "what happened to order X".

    Every field comes from the broker's own `order_detail` response. There
    is no default, no fallback and no derived value: `raw_status` is the
    venue's own status string verbatim, and `executed_quantity` /
    `executed_price` are `None` unless the broker actually reported them.
    The three mutually-exclusive booleans below are the ONLY interpretation
    applied anywhere, so there is exactly one place to look to see what this
    system believes a broker status means."""

    order_id: str
    raw_status: str
    executed_quantity: Decimal | None = None
    executed_price: Decimal | None = None
    updated_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.raw_status in _TERMINAL_STATUSES

    @property
    def executed(self) -> bool:
        """The order is finished AND the broker reported a real quantity and
        a real price for it. Both are required: a terminal status carrying
        no price is not a fill this system will record, because completing
        it would mean inventing the one number a fill is actually about."""
        return (
            self.terminal
            and self.executed_quantity is not None
            and self.executed_quantity > 0
            and self.executed_price is not None
        )

    @property
    def closed_unfilled(self) -> bool:
        """Finished with nothing to record: cancelled, expired, or rejected
        by the venue itself."""
        return self.terminal and not self.executed

    @property
    def still_open(self) -> bool:
        """Not finished - genuinely still working, or carrying a status this
        code does not classify. Either way the correct action is to change
        nothing and look again next cycle."""
        return not self.terminal


class LiveBrokerAdapter:
    """Implements `BrokerAdapter` against a real Longbridge trading account.

    Account state is fetched from the broker ONCE per adapter instance and
    cached for that instance's lifetime. The route constructs one adapter
    per request, so within a single trade the Risk Engine, the Portfolio
    Manager, and the buying-power check all reason about the same
    positions and the same cash - a second mid-trade fetch could hand two
    gates two different books, which is precisely the inconsistency
    docs/TRADING_SAFETY.md's fail-closed section exists to prevent.
    """

    def __init__(self, client: LiveTradeClient, *, currency: str = "USD") -> None:
        self._client = client
        self._currency = currency
        self._cash: Decimal | None = None
        self._positions: dict[str, Decimal] | None = None
        self.fills: list[Fill] = []

    # -- account snapshot -------------------------------------------------

    def _load(self) -> None:
        if self._cash is not None and self._positions is not None:
            return

        try:
            balances = self._client.account_balance()
        except Exception as exc:
            raise LiveBrokerError(f"Could not read live account balance: {exc}") from exc

        cash: Decimal | None = None
        for balance in balances or []:
            if getattr(balance, "currency", None) == self._currency:
                cash = Decimal(str(balance.total_cash))
                break
        if cash is None:
            raise LiveBrokerError(
                f"Live account has no {self._currency} balance; refusing to value the "
                "account rather than substituting another currency's cash."
            )

        try:
            response = self._client.stock_positions()
        except Exception as exc:
            raise LiveBrokerError(f"Could not read live stock positions: {exc}") from exc

        positions: dict[str, Decimal] = {}
        for channel in getattr(response, "channels", None) or []:
            for position in getattr(channel, "positions", None) or []:
                quantity = Decimal(str(position.quantity))
                positions[position.symbol] = positions.get(position.symbol, Decimal(0)) + quantity

        self._cash = cash
        self._positions = positions

    @property
    def cash(self) -> Decimal:
        self._load()
        assert self._cash is not None
        return self._cash

    @property
    def positions(self) -> dict[str, Decimal]:
        """A copy, same contract as PaperBrokerAdapter.positions."""
        self._load()
        assert self._positions is not None
        return dict(self._positions)

    def get_account_state(self, *, marks: dict[str, Decimal]) -> AccountState:
        """Identical semantics to `PaperBrokerAdapter.get_account_state`:
        a missing mark for an open position is an error the caller must
        resolve, never a gap filled with a guess (spec Sec57)."""
        positions = self.positions
        exposure = Decimal(0)
        positions_value = Decimal(0)
        for symbol, quantity in positions.items():
            if quantity == 0:
                continue
            if symbol not in marks:
                raise ValueError(
                    f"No mark supplied for open position {symbol}; cannot value account."
                )
            exposure += abs(quantity) * marks[symbol]
            positions_value += quantity * marks[symbol]

        cash = self.cash
        return AccountState(
            equity=cash + positions_value, cash=cash, current_exposure=exposure
        )

    # -- order status ------------------------------------------------------

    def cancel_order(self, order_id: str) -> LiveOrderStatus:
        """Phase 84 (D101): ask the venue to cancel, then read back what it
        says. The returned status is the broker's, not an assumption — a
        cancel can race a fill, and the answer to "did it cancel?" is
        whatever `order_detail` reports afterwards."""
        try:
            self._client.cancel_order(order_id)
        except Exception as exc:
            raise LiveBrokerError(f"Cancel of live order {order_id} failed: {exc}") from exc
        self._snapshot = None
        return self.get_order_status(order_id)

    def get_order_status(self, order_id: str) -> LiveOrderStatus:
        """Read one live order back from the broker (Phase 49, D066).

        This is the same real `TradeContext.order_detail()` call
        `submit_order()` already makes, promoted to a public method so the
        reconciler (apps/api/app/execution/reconciliation.py) can ask about
        an order it did not itself submit. No new SDK surface and no second
        client: the reconciler talks to the broker through this adapter, so
        there is exactly one place in the codebase that maps a Longbridge
        order-detail response onto this system's types.

        Raises `LiveBrokerError` when the broker cannot be reached or its
        response cannot be understood. It never returns a degraded or
        assumed status - the caller's only correct response to a failure
        here is to leave the order alone and try again later, which is what
        the reconciler does.

        Deliberately does NOT touch the cached account snapshot: this is a
        read about one order, not a statement about the book.
        """
        try:
            detail = self._client.order_detail(order_id)
        except Exception as exc:
            raise LiveBrokerError(
                f"Could not read the status of live order {order_id}: {exc}"
            ) from exc

        raw_status = getattr(detail.status, "name", None) or str(detail.status)

        executed_quantity_raw = getattr(detail, "executed_quantity", None)
        executed_price_raw = getattr(detail, "executed_price", None)
        try:
            executed_quantity = (
                None if executed_quantity_raw is None else Decimal(str(executed_quantity_raw))
            )
            executed_price = (
                None if executed_price_raw is None else Decimal(str(executed_price_raw))
            )
        except (ArithmeticError, ValueError) as exc:
            # A response we cannot parse is a response we do not understand.
            # Failing here leaves the order untouched and under observation,
            # which is strictly better than coercing a garbage value into a
            # permanent fill row.
            raise LiveBrokerError(
                f"Live order {order_id} reported an unparseable execution "
                f"quantity/price: {exc}"
            ) from exc

        updated_at = getattr(detail, "updated_at", None)
        if not isinstance(updated_at, datetime):
            updated_at = None
        elif updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)

        return LiveOrderStatus(
            order_id=order_id,
            raw_status=raw_status,
            executed_quantity=executed_quantity,
            executed_price=executed_price,
            updated_at=updated_at,
        )

    # -- order submission -------------------------------------------------

    def submit_order(self, order: OrderRequest, *, market_price: Decimal) -> Fill:
        """`market_price` is accepted only to keep this signature identical
        to `PaperBrokerAdapter.submit_order`'s. It is NOT sent to the broker
        (this is a market order; the venue sets the price) and is never
        used to construct the returned `Fill` - see this module's
        "No fabricated fills" note."""
        del market_price  # deliberately unused; see docstring

        from longport.openapi import OrderSide, OrderType, TimeInForceType

        if order.quantity <= 0:
            raise LiveBrokerError(f"Refusing to submit a non-positive quantity: {order.quantity}.")
        if order.quantity != order.quantity.to_integral_value():
            raise LiveBrokerError(
                f"Refusing to submit a fractional live quantity ({order.quantity}). "
                "Fractional-share support is not implemented and must not be rounded silently."
            )

        side = OrderSide.Buy if order.side is Side.BUY else OrderSide.Sell

        try:
            if order.order_type == "limit":
                # Phase 84 (D101): a real limit order. The venue may rest it;
                # the not-executed branch below then records it as
                # SUBMITTED_UNCONFIRMED for the reconciler, exactly as an
                # unfilled market order is.
                response = self._client.submit_order(
                    symbol=order.symbol,
                    order_type=OrderType.LO,
                    side=side,
                    submitted_quantity=order.quantity,
                    time_in_force=TimeInForceType.Day,
                    submitted_price=order.limit_price,
                )
            else:
                response = self._client.submit_order(
                    symbol=order.symbol,
                    order_type=OrderType.MO,
                    side=side,
                    submitted_quantity=order.quantity,
                    time_in_force=TimeInForceType.Day,
                )
        except Exception as exc:
            raise LiveBrokerError(
                f"Live order submission failed for {order.symbol!r}: {exc}"
            ) from exc

        order_id = str(response.order_id)

        try:
            detail = self._client.order_detail(order_id)
        except Exception as exc:
            # The order EXISTS at the broker; we simply cannot see its
            # state. Surfacing the id is the whole point of this branch.
            raise LiveOrderNotFilledError(
                f"Live order {order_id} was submitted but its status could not be read: {exc}",
                order_id=order_id,
            ) from exc

        status = getattr(detail.status, "name", None) or str(detail.status)
        executed_quantity = Decimal(str(detail.executed_quantity or 0))
        executed_price_raw = detail.executed_price

        if status not in _EXECUTED_STATUSES or executed_quantity <= 0 or executed_price_raw is None:
            raise LiveOrderNotFilledError(
                f"Live order {order_id} for {order.symbol!r} is not executed "
                f"(status={status}, executed_quantity={executed_quantity}). No fill is "
                "reported rather than assuming one.",
                order_id=order_id,
            )

        filled_at = getattr(detail, "updated_at", None)
        if not isinstance(filled_at, datetime):
            filled_at = datetime.now(UTC)
        elif filled_at.tzinfo is None:
            filled_at = filled_at.replace(tzinfo=UTC)

        fill = Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=executed_quantity,
            fill_price=Decimal(str(executed_price_raw)),
            filled_at=filled_at,
        )
        self.fills.append(fill)

        # The cached snapshot is now stale by exactly this fill. Drop it
        # rather than patching it locally: the broker, not this process, is
        # the source of truth for a live book (spec Sec62).
        self._cash = None
        self._positions = None
        return fill


def build_live_broker_adapter(settings: Settings) -> LiveBrokerAdapter | None:
    """Returns `None` - never a partially-configured or simulated stand-in -
    unless live trading is fully and explicitly enabled AND all three live
    credentials are present together.

    The credential trio is all-or-nothing for the same reason D015's
    market-data trio is: a partial configuration is a mistake, and guessing
    at the missing piece is how a system ends up pointed somewhere nobody
    intended. The `LONGPORT_LIVE_*` credentials are deliberately SEPARATE
    settings from the read-only `LONGPORT_*` quote credentials - a quote
    key must never silently become a trading key.
    """
    if settings.trading_mode is not TradingMode.LIVE:
        return None
    if not settings.live_trading_enabled:
        return None
    if not (
        settings.longport_live_app_key
        and settings.longport_live_app_secret
        and settings.longport_live_access_token
    ):
        return None

    from longport.openapi import Config, TradeContext

    config = Config.from_apikey(
        app_key=settings.longport_live_app_key,
        app_secret=settings.longport_live_app_secret,
        access_token=settings.longport_live_access_token,
    )
    return LiveBrokerAdapter(TradeContext(config), currency=settings.live_account_currency)
