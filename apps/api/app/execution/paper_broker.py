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

from apps.api.app.execution.broker import Fill, OrderRequest
from apps.api.app.risk.models import AccountState, Side


class InsufficientFundsError(Exception):
    pass


class InsufficientPositionError(Exception):
    pass


class PaperBrokerAdapter:
    def __init__(
        self, *, starting_cash: Decimal, positions: dict[str, Decimal] | None = None
    ) -> None:
        self._cash = starting_cash
        self._positions: dict[str, Decimal] = dict(positions) if positions else {}
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
                raise InsufficientPositionError(
                    f"Cannot sell {order.quantity} of {order.symbol}; only {held} held. "
                    "Short selling is not supported by the paper broker."
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
