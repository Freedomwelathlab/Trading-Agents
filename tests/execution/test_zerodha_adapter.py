"""The Zerodha (Kite Connect) adapter (Phase 110, D137).

Against a stub client, never a real key. Response shapes are the ones in
Zerodha's published documentation. What is asserted is what Kite's
response alone does not decide:

* Success needs BOTH the HTTP status and the envelope to say so, and an
  expired daily token is named as that.
* Held = settled + T+1 holdings + today's delivery buys; a sale already
  taken out of holdings is not subtracted twice.
* An account holding something this adapter cannot value is refused.
* A partial fill reports what was executed; an unexecuted order raises the
  port's `OrderNotConfirmedError` with Kite's order id; a rejected one is
  an error carrying Kite's reason.
* Validate-only prices the order and places nothing.
* A symbol without an exchange is refused, never defaulted.
"""

import hashlib
from decimal import Decimal

import pytest

from apps.api.app.execution.adapters.zerodha import (
    ZerodhaAdapter,
    ZerodhaError,
    ZerodhaOrderNotConfirmedError,
    session_checksum,
    split_symbol,
)
from apps.api.app.execution.broker import OrderNotConfirmedError, OrderRequest
from apps.api.app.risk.models import Side
from scripts.zerodha_login import request_token_from

D = Decimal


def ok(data):
    return 200, {"status": "success", "data": data}


def refused(status, kind, message):
    return status, {"status": "error", "message": message, "error_type": kind}


MARGINS = ok({"enabled": True, "net": 99725.05, "available": {"cash": 245431.6}})
HOLDINGS = ok([
    {"tradingsymbol": "INFY", "exchange": "NSE", "quantity": 10, "t1_quantity": 2,
     "product": "CNC"},
    {"tradingsymbol": "SBIN", "exchange": "BSE", "quantity": 16, "t1_quantity": 0,
     "product": "CNC"},
])
NO_POSITIONS = ok({"net": [], "day": []})


class StubClient:
    """Answers by `METHOD path` and records every call."""

    def __init__(self, **routes):
        self.routes = {
            "GET /user/margins/equity": MARGINS,
            "GET /portfolio/holdings": HOLDINGS,
            "GET /portfolio/positions": NO_POSITIONS,
        }
        self.routes.update(routes)
        self.calls: list[tuple[str, object]] = []

    def _answer(self, key, sent=None):
        self.calls.append((key, sent))
        if key not in self.routes:
            raise AssertionError(f"no stub for {key}")
        return self.routes[key]

    def get(self, path):
        return self._answer(f"GET {path}")

    def post_form(self, path, data):
        return self._answer(f"POST {path}", dict(data))

    def post_json(self, path, payload):
        return self._answer(f"POST {path}", payload)

    def delete(self, path):
        return self._answer(f"DELETE {path}")

    def sent(self, key):
        return next(body for k, body in self.calls if k == key)

    def called(self, key):
        return any(k == key for k, _ in self.calls)


def adapter(validate_only=False, **routes):
    client = StubClient(**{k.replace("__", " ", 1): v for k, v in routes.items()})
    return ZerodhaAdapter(client, validate_only=validate_only), client


def buy(symbol="NSE:INFY", quantity="5", **over):
    return OrderRequest(symbol=symbol, side=Side.BUY, quantity=D(quantity), **over)


def history(*states):
    base = {"order_id": "151220000000000", "tradingsymbol": "INFY", "exchange": "NSE"}
    return ok([{**base, **state} for state in states])


# --- envelope ---------------------------------------------------------------


def test_an_expired_daily_token_is_named_as_that_and_not_an_empty_account():
    broker, _ = adapter(**{
        "GET__/user/margins/equity": refused(403, "TokenException", "Incorrect `api_key`"),
    })
    with pytest.raises(ZerodhaError, match="TOKEN_EXPIRED.*06:00 IST"):
        _ = broker.cash


def test_a_success_envelope_on_an_error_status_is_still_a_failure():
    broker, _ = adapter(**{"GET__/user/margins/equity": (500, {"status": "success", "data": {}})})
    with pytest.raises(ZerodhaError, match="HTTP 500"):
        _ = broker.cash


def test_a_body_that_is_not_an_object_is_an_error():
    broker, _ = adapter(**{"GET__/user/margins/equity": (502, None)})
    with pytest.raises(ZerodhaError, match="non-object body"):
        _ = broker.cash


# --- account ----------------------------------------------------------------


def test_cash_is_the_net_balance_and_holdings_include_t1_stock():
    broker, _ = adapter()
    assert broker.cash == D("99725.05")
    assert broker.positions == {"NSE:INFY": D(12), "BSE:SBIN": D(16)}


def test_a_missing_net_balance_is_an_error_not_zero_cash():
    broker, _ = adapter(**{"GET__/user/margins/equity": ok({"enabled": True})})
    with pytest.raises(ZerodhaError, match="absent balance"):
        _ = broker.cash


def test_todays_delivery_buy_counts_and_todays_sale_is_not_subtracted_twice():
    positions = ok({"net": [
        {"tradingsymbol": "TCS", "exchange": "NSE", "product": "CNC", "quantity": 3},
        # Sold from holdings today: Kite has already reduced the holding.
        {"tradingsymbol": "SBIN", "exchange": "BSE", "product": "CNC", "quantity": -4},
        {"tradingsymbol": "INFY", "exchange": "NSE", "product": "CNC", "quantity": 0},
    ]})
    broker, _ = adapter(**{"GET__/portfolio/positions": positions})
    assert broker.positions == {"NSE:INFY": D(12), "BSE:SBIN": D(16), "NSE:TCS": D(3)}


@pytest.mark.parametrize("product", ["MIS", "NRML"])
def test_an_account_holding_a_margin_position_is_refused_rather_than_misvalued(product):
    positions = ok({"net": [
        {"tradingsymbol": "NIFTY26NOVFUT", "exchange": "NFO", "product": product,
         "quantity": 50},
    ]})
    broker, _ = adapter(**{"GET__/portfolio/positions": positions})
    with pytest.raises(ZerodhaError, match=f"holds a {product} position"):
        _ = broker.positions


def test_valuing_the_account_needs_a_mark_for_every_held_symbol():
    broker, _ = adapter()
    with pytest.raises(ZerodhaError, match="No mark supplied for held symbol BSE:SBIN"):
        broker.get_account_state(marks={"NSE:INFY": D("1500")})
    state = broker.get_account_state(marks={"NSE:INFY": D("1500"), "BSE:SBIN": D("800")})
    assert state.cash == D("99725.05")
    assert state.current_exposure == D(12) * D("1500") + D(16) * D("800")
    assert state.equity == state.cash + state.current_exposure


# --- symbols ----------------------------------------------------------------


def test_a_symbol_needs_its_exchange_and_none_is_assumed():
    assert split_symbol("NSE:INFY") == ("NSE", "INFY")
    for bad in ("INFY", "INFY.IN", "NFO:NIFTY26NOVFUT", "NSE:"):
        with pytest.raises(ZerodhaError, match="EXCHANGE:TRADINGSYMBOL"):
            split_symbol(bad)


def test_a_bad_symbol_never_reaches_kite():
    broker, client = adapter()
    with pytest.raises(ZerodhaError, match="EXCHANGE:TRADINGSYMBOL"):
        broker.submit_order(buy(symbol="INFY"), market_price=D("1500"))
    assert client.calls == []


# --- orders -----------------------------------------------------------------


def test_a_market_order_is_sent_as_delivery_with_kites_price_protection():
    broker, client = adapter(**{
        "POST__/orders/regular": ok({"order_id": "151220000000000"}),
        "GET__/orders/151220000000000": history(
            {"status": "OPEN PENDING", "filled_quantity": 0, "average_price": 0},
            {"status": "COMPLETE", "filled_quantity": 5, "average_price": 1502.35},
        ),
    })
    fill = broker.submit_order(buy(), market_price=D("1500"))
    assert client.sent("POST /orders/regular") == {
        "exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": "BUY",
        "order_type": "MARKET", "quantity": "5", "product": "CNC", "validity": "DAY",
        "market_protection": "-1",
    }
    assert (fill.symbol, fill.quantity, fill.fill_price) == ("NSE:INFY", D(5), D("1502.35"))


def test_a_limit_order_carries_its_price_and_no_market_protection():
    broker, client = adapter(**{
        "POST__/orders/regular": ok({"order_id": "151220000000000"}),
        "GET__/orders/151220000000000": history(
            {"status": "COMPLETE", "filled_quantity": 5, "average_price": 1499.0},
        ),
    })
    order = OrderRequest(symbol="NSE:INFY", side=Side.SELL, quantity=D(5),
                         order_type="limit", limit_price=D("1499.50"))
    broker.submit_order(order, market_price=D("1500"))
    sent = client.sent("POST /orders/regular")
    assert sent["order_type"] == "LIMIT" and sent["price"] == "1499.50"
    assert sent["transaction_type"] == "SELL" and "market_protection" not in sent


def test_a_partial_fill_reports_the_executed_quantity_not_the_requested_one():
    broker, _ = adapter(**{
        "POST__/orders/regular": ok({"order_id": "151220000000000"}),
        "GET__/orders/151220000000000": history(
            {"status": "OPEN", "filled_quantity": 2, "average_price": 1501.0},
        ),
    })
    fill = broker.submit_order(buy(), market_price=D("1500"))
    assert fill.quantity == D(2)


def test_an_accepted_but_unexecuted_order_raises_the_ports_error_with_kites_id():
    broker, _ = adapter(**{
        "POST__/orders/regular": ok({"order_id": "151220000000000"}),
        "GET__/orders/151220000000000": history(
            {"status": "OPEN PENDING", "filled_quantity": 0, "average_price": 0},
        ),
    })
    with pytest.raises(OrderNotConfirmedError) as caught:
        broker.submit_order(buy(), market_price=D("1500"))
    assert isinstance(caught.value, ZerodhaOrderNotConfirmedError)
    assert caught.value.order_id == "151220000000000"


def test_a_rejected_order_is_an_error_carrying_kites_reason():
    broker, _ = adapter(**{
        "POST__/orders/regular": ok({"order_id": "151220000000000"}),
        "GET__/orders/151220000000000": history(
            {"status": "REJECTED", "filled_quantity": 0, "average_price": 0,
             "status_message": "Insufficient funds. Required margin is 7510.00"},
        ),
    })
    with pytest.raises(ZerodhaError, match="rejected order.*Insufficient funds") as caught:
        broker.submit_order(buy(), market_price=D("1500"))
    assert not isinstance(caught.value, OrderNotConfirmedError)


def test_an_execution_at_a_non_positive_price_is_refused_rather_than_recorded():
    broker, _ = adapter(**{
        "POST__/orders/regular": ok({"order_id": "151220000000000"}),
        "GET__/orders/151220000000000": history(
            {"status": "COMPLETE", "filled_quantity": 5, "average_price": 0},
        ),
    })
    with pytest.raises(ZerodhaError, match="non-positive average price"):
        broker.submit_order(buy(), market_price=D("1500"))


def test_kites_own_refusal_of_an_order_is_surfaced():
    broker, _ = adapter(**{
        "POST__/orders/regular": refused(400, "MarginException", "Insufficient funds"),
    })
    with pytest.raises(ZerodhaError, match="MarginException.*Insufficient funds"):
        broker.submit_order(buy(), market_price=D("1500"))


@pytest.mark.parametrize("quantity", ["0", "-1", "2.5"])
def test_a_non_positive_or_fractional_quantity_never_reaches_kite(quantity):
    broker, client = adapter()
    with pytest.raises(ZerodhaError):
        broker.submit_order(
            OrderRequest.model_construct(
                symbol="NSE:INFY", side=Side.BUY, quantity=D(quantity),
                order_type="market", limit_price=None,
            ),
            market_price=D("1500"),
        )
    assert client.calls == []


def test_validate_only_prices_the_order_and_places_nothing():
    broker, client = adapter(validate_only=True, **{
        "POST__/margins/orders": ok([{"type": "equity", "tradingsymbol": "INFY", "total": 7510}]),
    })
    with pytest.raises(ZerodhaError, match="VALIDATE_ONLY.*7510.*did NOT place"):
        broker.submit_order(buy(), market_price=D("1500"))
    assert client.sent("POST /margins/orders") == [{
        "exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": "BUY",
        "order_type": "MARKET", "quantity": 5, "product": "CNC", "variety": "regular",
        "price": 0.0, "trigger_price": 0,
    }]
    assert not client.called("POST /orders/regular")


def test_a_new_adapter_is_validate_only_unless_told_otherwise():
    assert ZerodhaAdapter(StubClient())._validate_only is True


def test_cancelling_reports_kites_answer():
    broker, _ = adapter(**{
        "DELETE__/orders/regular/151220000000000": ok({"order_id": "151220000000000"}),
    })
    assert broker.cancel_order("151220000000000") is True


# --- login helper -----------------------------------------------------------


def test_the_session_checksum_is_sha256_of_key_token_secret_in_that_order():
    expected = hashlib.sha256(b"kkk" + b"ttt" + b"sss").hexdigest()
    assert session_checksum("kkk", "ttt", "sss") == expected
    assert session_checksum("kkk", "sss", "ttt") != expected


def test_the_request_token_is_read_from_a_pasted_redirect_url_or_taken_bare():
    url = "https://example.test/cb?action=login&status=success&request_token=abc123XYZ"
    assert request_token_from(url) == "abc123XYZ"
    assert request_token_from("  abc123XYZ \n") == "abc123XYZ"
    assert request_token_from("https://example.test/cb?request_token=") == ""
