"""Binance spot adapter (Phase 97, D116), against a stub - no key needed.

The rules under test are the ones a real account would make expensive to
get wrong: which venue the key is sent to, that validate-only is the
default wherever the money is real, that a location refusal is not
reported as a credential one, and that nothing is ever filled at a price
Binance did not give.
"""

from decimal import Decimal
from typing import Any

import pytest

from apps.api.app.execution.adapters.binance import (
    HOSTS,
    BinanceAdapter,
    BinanceError,
    BinanceOrderNotConfirmedError,
    binance_signature,
    build_binance_adapter,
)
from apps.api.app.execution.broker import OrderNotConfirmedError, OrderRequest
from apps.api.app.risk.models import Side

ACCOUNT = {
    "balances": [
        {"asset": "USDT", "free": "250.5", "locked": "10"},
        {"asset": "BTC", "free": "0.01", "locked": "0.002"},
        {"asset": "ETH", "free": "0", "locked": "0"},
    ]
}


class Stub:
    def __init__(self, routes: dict[tuple[str, str], tuple[int, Any]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def signed(self, method: str, path: str, params: Any) -> tuple[int, Any]:
        self.calls.append((method, path, dict(params)))
        return self.routes[(method, path)]

    def public(self, path: str, params: Any) -> tuple[int, Any]:
        raise AssertionError("no public call expected")


def adapter(stub: Stub, **kw: Any) -> BinanceAdapter:
    kw.setdefault("environment", "TESTNET")
    kw.setdefault("validate_only", False)
    return BinanceAdapter(stub, **kw)


def test_the_signature_matches_binances_own_worked_example():
    # The SIGNED-endpoint example in Binance's API documentation.
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    query = (
        "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
        "&recvWindow=5000&timestamp=1499827319559"
    )
    assert binance_signature(query, secret) == (
        "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    )


def test_cash_is_the_free_quote_balance_and_every_other_holding_is_a_position():
    a = adapter(Stub({("GET", "/api/v3/account"): (200, ACCOUNT)}))
    assert a.cash == Decimal("250.5")  # locked USDT is committed to orders
    assert a.positions == {"BTC": Decimal("0.012")}  # a locked coin is still held


def test_a_missing_quote_row_is_an_error_not_a_zero():
    stub = Stub({("GET", "/api/v3/account"): (200, {"balances": [ACCOUNT["balances"][1]]})})
    with pytest.raises(BinanceError) as err:
        _ = adapter(stub).cash
    assert "absent balance, not a zero" in str(err.value)


def test_valuing_needs_a_mark_for_every_holding():
    a = adapter(Stub({("GET", "/api/v3/account"): (200, ACCOUNT)}))
    with pytest.raises(BinanceError):
        a.get_account_state(marks={})
    state = a.get_account_state(marks={"BTC": Decimal("60000")})
    assert state.equity == Decimal("250.5") + Decimal("0.012") * 60000


def test_http_451_is_reported_as_location_not_credentials():
    stub = Stub({("GET", "/api/v3/account"): (451, {"code": 0, "msg": "restricted"})})
    with pytest.raises(BinanceError) as err:
        _ = adapter(stub, environment="LIVE").cash
    assert "LOCATION" in str(err.value)
    assert "not a credential problem" in str(err.value)


def test_a_rejected_key_keeps_binances_code_and_says_what_to_check():
    stub = Stub(
        {
            ("GET", "/api/v3/account"): (
                401,
                {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."},
            )
        }
    )
    with pytest.raises(BinanceError) as err:
        _ = adapter(stub).cash
    assert "-2015" in str(err.value)
    assert "BINANCE_ENVIRONMENT" in str(err.value)


def test_validate_only_calls_the_test_endpoint_and_never_reports_a_fill():
    stub = Stub({("POST", "/api/v3/order/test"): (200, {})})
    a = adapter(stub, environment="LIVE", validate_only=True)
    with pytest.raises(BinanceError) as err:
        a.submit_order(
            OrderRequest(symbol="BTCUSDT", side=Side.BUY, quantity=Decimal("0.001")),
            market_price=Decimal("60000"),
        )
    assert "VALIDATE_ONLY" in str(err.value)
    assert [c[1] for c in stub.calls] == ["/api/v3/order/test"]


def test_a_fill_uses_binances_executed_quantity_and_average_price():
    stub = Stub(
        {
            ("POST", "/api/v3/order"): (
                200,
                {
                    "orderId": 42,
                    "status": "PARTIALLY_FILLED",
                    "executedQty": "0.0005",
                    "cummulativeQuoteQty": "30.5",
                },
            )
        }
    )
    fill = adapter(stub).submit_order(
        OrderRequest(symbol="BTCUSDT", side=Side.BUY, quantity=Decimal("0.001")),
        market_price=Decimal("1"),  # never used as a price
    )
    assert fill.quantity == Decimal("0.0005")
    assert fill.fill_price == Decimal("61000")
    method, path, params = stub.calls[0]
    assert (method, path) == ("POST", "/api/v3/order")
    assert params["type"] == "MARKET" and params["side"] == "BUY"


def test_an_unexecuted_order_raises_the_reconcilable_type_with_symbol_and_id():
    stub = Stub(
        {("POST", "/api/v3/order"): (200, {"orderId": 7, "status": "NEW", "executedQty": "0"})}
    )
    with pytest.raises(BinanceOrderNotConfirmedError) as err:
        adapter(stub).submit_order(
            OrderRequest(
                symbol="ETHUSDT",
                side=Side.SELL,
                quantity=Decimal("1"),
                order_type="limit",
                limit_price=Decimal("5000"),
            ),
            market_price=Decimal("3000"),
        )
    assert isinstance(err.value, OrderNotConfirmedError)
    assert err.value.order_id == "ETHUSDT:7"
    params = stub.calls[0][2]
    assert params["price"] == "5000" and params["timeInForce"] == "GTC"


def test_cancel_addresses_the_order_by_symbol_and_id():
    stub = Stub({("DELETE", "/api/v3/order"): (200, {"status": "CANCELED"})})
    assert adapter(stub).cancel_order("ETHUSDT:7") is True
    assert stub.calls[0][2] == {"symbol": "ETHUSDT", "orderId": "7"}
    with pytest.raises(BinanceError):
        adapter(stub).cancel_order("7")


def test_the_factory_requires_an_environment_and_defaults_by_where_the_money_is():
    base = {"api_key": "k", "api_secret": "s"}
    with pytest.raises(BinanceError) as err:
        build_binance_adapter(base)
    assert "no default" in str(err.value)
    with pytest.raises(BinanceError):
        build_binance_adapter({**base, "environment": "MAINNET"})

    assert build_binance_adapter({**base, "environment": "LIVE"})._validate_only is True
    assert build_binance_adapter({**base, "environment": "US"})._validate_only is True
    assert build_binance_adapter({**base, "environment": "testnet"})._validate_only is False
    armed = build_binance_adapter({**base, "environment": "LIVE", "live_orders": "true"})
    assert armed._validate_only is False


def test_each_environment_is_its_own_host():
    assert HOSTS["TESTNET"] == "https://testnet.binance.vision"
    assert HOSTS["US"] == "https://api.binance.us"
    us = build_binance_adapter({"api_key": "k", "api_secret": "s", "environment": "US"})
    assert us._quote == "USD"
