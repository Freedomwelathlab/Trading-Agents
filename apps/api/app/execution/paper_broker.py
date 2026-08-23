"""Deterministic in-memory simulated exchange. No network call, no real
money, no fabricated prices - every fill uses the market_price the caller
supplies (from a real market-data snapshot upstream), never an invented one.

Not persisted (in-process only). Persistence is explicitly out of scope for
this phase - see docs/DECISIONS.md D005.
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
    def __init__(self, *, starting_cash: Decimal) -> None:
        self._cash = starting_cash
        self._positions: dict[str, Decimal] = {}
        self.fills: list[Fill] = []

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
