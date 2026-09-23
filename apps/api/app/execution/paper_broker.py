"""Deterministic simulated exchange. No network call, no real money, no
fabricated prices - every fill uses the market_price the caller supplies
(from a real market-data snapshot upstream), never an invented one.

This class itself still holds cash/positions purely in memory and knows
nothing about a database - apps/api/app/execution/persistence.py loads a
snapshot into one of these at the start of a request and saves it back at
the end (see docs/DECISIONS.md D014). That wrapping split mirrors D006's
submit_trade / submit_trade_and_record pattern: the pure object stays
simple and directly unit-testable; persistence is a layer around it, not
a change to it.
"""

from datetime import UTC, datetime
from decimal import Decimal

from apps.api.app.execution.broker import Fill, OrderRequest, OrderWouldRestError
from apps.api.app.risk.models import AccountState, Side


class InsufficientFundsError(Exception):
    pass


class InsufficientPositionError(Exception):
    pass


class PaperBrokerAdapter:
    """
    `allow_short` (Phase 87, D106) is opt-in per adapter, not a global
    behaviour change. Every existing caller - agent trades, strategy
    deployments, the backtest replay - is long-only, and for them a sell
    beyond the held quantity is a bug that must keep raising rather than
    quietly open a short position nobody asked for. Only the intraday bot,
    and only when its operator ticked `allow_short`, constructs one of
    these with shorting enabled.

    **Short collateral is 100% cash.** A short's loss is unbounded, and
    this simulator has no margin model, no borrow availability and no
    financing cost. Rather than invent one, it refuses to open a short
    whose notional is not covered by cash already in the account. That is
    stricter than any real broker, which is the correct direction for a
    simulator to be wrong in: a paper account that can open positions the
    real one would reject produces a track record the live account could
    not have earned.
    """

    def __init__(
        self,
        *,
        starting_cash: Decimal,
        positions: dict[str, Decimal] | None = None,
        allow_short: bool = False,
    ) -> None:
        self._cash = starting_cash
        self._positions: dict[str, Decimal] = dict(positions) if positions else {}
        self._allow_short = allow_short
        self.fills: list[Fill] = []

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def positions(self) -> dict[str, Decimal]:
        """A copy - callers must not mutate a broker's positions except
        through submit_order()."""
        return dict(self._positions)

    def submit_order(self, order: OrderRequest, *, market_price: Decimal) -> Fill:
        if order.order_type == "limit":
            # Phase 84 (D101): no order book here, so only a MARKETABLE
            # limit can be honoured, and it fills at the market — which is
            # what a marketable limit gets at a real venue too.
            assert order.limit_price is not None  # nosec - validated upstream
            marketable = (
                order.limit_price >= market_price
                if order.side is Side.BUY
                else order.limit_price <= market_price
            )
            if not marketable:
                raise OrderWouldRestError(
                    f"{order.side.value} limit {order.limit_price} is not marketable at "
                    f"{market_price}; the paper broker cannot hold a resting order and "
                    f"will not fabricate a later fill."
                )
        notional = order.quantity * market_price

        if order.side is Side.BUY:
            if notional > self._cash:
                raise InsufficientFundsError(
                    f"Order notional {notional} exceeds available cash {self._cash}."
                )
            self._cash -= notional
            held = self._positions.get(order.symbol, Decimal(0))
            self._positions[order.symbol] = held + order.quantity
        else:
            held = self._positions.get(order.symbol, Decimal(0))
            if order.quantity > held:
                if not self._allow_short:
                    raise InsufficientPositionError(
                        f"Cannot sell {order.quantity} of {order.symbol}; only {held} held. "
                        "Short selling is not enabled on this paper broker."
                    )
                # Only the part that goes past what is held is a new short,
                # and only that part needs collateral - selling 10 held
                # shares and shorting 2 is not a 12-share short.
                shorted = (order.quantity - max(held, Decimal(0))) * market_price
                if shorted > self._cash:
                    raise InsufficientFundsError(
                        f"Short notional {shorted} exceeds available cash {self._cash}; "
                        "this paper broker collateralises shorts at 100% cash because it "
                        "has no margin model to price the risk with."
                    )
            self._positions[order.symbol] = held - order.quantity
            self._cash += notional

        fill = Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            fill_price=market_price,
            filled_at=datetime.now(UTC),
        )
        self.fills.append(fill)
        return fill

    def get_account_state(self, *, marks: dict[str, Decimal]) -> AccountState:
        """`marks` must carry a current price for every open position - a
        missing mark is a data-availability problem the caller must resolve
        (spec Sec57: never fabricate a mark to fill a gap)."""
        exposure = Decimal(0)
        for symbol, quantity in self._positions.items():
            if quantity == 0:
                continue
            if symbol not in marks:
                raise ValueError(
                    f"No mark supplied for open position {symbol}; cannot value account."
                )
            exposure += abs(quantity) * marks[symbol]

        positions_value = sum(
            (self._positions[s] * marks[s] for s in self._positions if self._positions[s] != 0),
            start=Decimal(0),
        )
        equity = self._cash + positions_value
        return AccountState(equity=equity, cash=self._cash, current_exposure=exposure)
