from decimal import Decimal

import pytest

from apps.api.app.execution.broker import OrderRequest
from apps.api.app.execution.paper_broker import (
    InsufficientFundsError,
    InsufficientPositionError,
    PaperBrokerAdapter,
)
from apps.api.app.risk.models import Side


def test_market_buy_fills_and_debits_cash():
    broker = PaperBrokerAdapter(starting_cash=Decimal(10_000))
    fill = broker.submit_order(
        OrderRequest(symbol="AAPL", side=Side.BUY, quantity=Decimal(10)),
        market_price=Decimal(100),
    )
    assert fill.fill_price == Decimal(100)
    assert fill.quantity == Decimal(10)

    account = broker.get_account_state(marks={"AAPL": Decimal(100)})
    assert account.cash == Decimal(9_000)
    assert account.equity == Decimal(10_000)
    assert account.current_exposure == Decimal(1_000)


def test_buy_exceeding_cash_raises_insufficient_funds():
    broker = PaperBrokerAdapter(starting_cash=Decimal(500))
    with pytest.raises(InsufficientFundsError):
        broker.submit_order(
            OrderRequest(symbol="AAPL", side=Side.BUY, quantity=Decimal(10)),
            market_price=Decimal(100),
        )


def test_sell_reduces_position_and_credits_cash():
    broker = PaperBrokerAdapter(starting_cash=Decimal(10_000))
    broker.submit_order(
        OrderRequest(symbol="AAPL", side=Side.BUY, quantity=Decimal(10)),
        market_price=Decimal(100),
    )
    broker.submit_order(
        OrderRequest(symbol="AAPL", side=Side.SELL, quantity=Decimal(4)),
        market_price=Decimal(110),
    )
    account = broker.get_account_state(marks={"AAPL": Decimal(110)})
    assert account.cash == Decimal(9_000) + Decimal(440)
    assert account.current_exposure == Decimal(6) * Decimal(110)


def test_short_selling_is_not_supported():
    broker = PaperBrokerAdapter(starting_cash=Decimal(10_000))
    with pytest.raises(InsufficientPositionError):
        broker.submit_order(
            OrderRequest(symbol="AAPL", side=Side.SELL, quantity=Decimal(1)),
            market_price=Decimal(100),
        )


def test_get_account_state_requires_a_mark_for_every_open_position():
    broker = PaperBrokerAdapter(starting_cash=Decimal(10_000))
    broker.submit_order(
        OrderRequest(symbol="AAPL", side=Side.BUY, quantity=Decimal(10)),
        market_price=Decimal(100),
    )
    with pytest.raises(ValueError, match="No mark supplied"):
        broker.get_account_state(marks={})
