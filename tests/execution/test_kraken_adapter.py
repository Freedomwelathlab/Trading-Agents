"""The Kraken spot adapter (Phase 92, D111).

Against a stub client, never a real key — the same discipline every vendor
adapter here follows. What is asserted is the set of decisions Kraken's
response alone does not determine:

* HTTP 200 with a non-empty `error` array is a FAILURE. Kraken's ordinary
  refusal shape looks like success to anything that reads status codes.
* A partial fill reports what was EXECUTED, not what was requested.
* An accepted-but-unexecuted order raises the port's own
  `OrderNotConfirmedError` carrying Kraken's txid, so the existing
  reconciler resolves it with no Kraken-specific branch.
* The signature is computed the one way Kraken accepts — a wrong signature
  and a wrong key are indistinguishable at the venue.
* Validate-only places nothing, and says so loudly enough that it cannot
  be mistaken for a fill.
"""

import base64
import hashlib
import hmac
import urllib.parse
from decimal import Decimal

import pytest

from apps.api.app.execution.adapters.kraken import (
    KrakenAdapter,
    KrakenError,
    KrakenOrderNotConfirmedError,
    kraken_signature,
)
from apps.api.app.execution.broker import OrderNotConfirmedError, OrderRequest
from apps.api.app.risk.models import Side

D = Decimal
SECRET = base64.b64encode(b"a-test-secret-32-bytes-long-----").decode()


class StubClient:
    """Returns a queued body per path and records what it was sent."""

    def __init__(self, **by_path):
        self._by_path = by_path
        self.calls: list[tuple[str, dict]] = []

    def private(self, path, data):
        self.calls.append((path, dict(data)))
        key = path.rsplit("/", 1)[-1]
        body = self._by_path.get(key)
        if body is None:
            raise AssertionError(f"no stub for {path}")
        return body() if callable(body) else body

    def public(self, path, params):  # pragma: no cover - unused by these tests
        raise AssertionError("public() not expected here")


def ok(result):
    return {"error": [], "result": result}


BALANCE = ok({"ZUSD": "10000.5000", "XXBT": "0.2500", "XETH": "0.0000"})


def adapter(**over) -> KrakenAdapter:
    stubs = dict(Balance=BALANCE)
    stubs.update(over.pop("stubs", {}))
    return KrakenAdapter(StubClient(**stubs), validate_only=False, **over)


def buy(**over) -> OrderRequest:
    base = dict(symbol="XBTUSD", side=Side.BUY, quantity=D("0.01"))
    base.update(over)
    return OrderRequest(**base)  # type: ignore[arg-type]


# --- the envelope -----------------------------------------------------------


def test_a_200_carrying_an_error_array_is_a_failure_not_a_success():
    # Kraken's ordinary refusal is HTTP 200 with `error` populated. Reading
    # the status code alone would treat "Unknown asset pair" as a fill.
    a = KrakenAdapter(
        StubClient(Balance={"error": ["EQuery:Unknown asset pair"], "result": {}}),
        validate_only=False,
    )
    with pytest.raises(KrakenError) as err:
        _ = a.cash
    assert "Unknown asset pair" in str(err.value)


def test_a_body_with_no_result_is_an_error_rather_than_an_empty_account():
    a = KrakenAdapter(StubClient(Balance={"error": []}), validate_only=False)
    with pytest.raises(KrakenError):
        _ = a.cash


# --- account ----------------------------------------------------------------


def test_the_quote_currency_is_cash_and_everything_else_is_a_position():
    a = adapter()
    assert a.cash == D("10000.5000")
    # ZUSD resolved to USD cash; the zero ETH balance is not a position.
    assert a.positions == {"XXBT": D("0.2500")}


def test_an_account_with_no_quote_balance_is_an_error_not_zero_cash():
    a = KrakenAdapter(StubClient(Balance=ok({"XXBT": "0.25"})), validate_only=False)
    with pytest.raises(KrakenError) as err:
        _ = a.cash
    assert "absent balance, not a zero one" in str(err.value)


def test_valuing_the_account_needs_a_mark_for_every_held_asset():
    a = adapter()
    with pytest.raises(KrakenError) as err:
        a.get_account_state(marks={})
    assert "No mark supplied" in str(err.value)

    state = a.get_account_state(marks={"XXBT": D("60000")})
    assert state.cash == D("10000.5000")
    assert state.equity == D("10000.5000") + D("0.25") * D("60000")
    assert state.current_exposure == D("15000.000")


# --- orders -----------------------------------------------------------------


def test_a_filled_order_reports_the_executed_quantity_not_the_requested_one():
    # A partial fill recorded at full size is a position the account does
    # not hold.
    a = adapter(
        stubs={
            "AddOrder": ok({"txid": ["OXYZ-1"], "descr": {"order": "buy 0.01 XBTUSD"}}),
            "QueryOrders": ok(
                {"OXYZ-1": {"status": "closed", "vol_exec": "0.004", "price": "61234.5"}}
            ),
        }
    )
    fill = a.submit_order(buy(quantity=D("0.01")), market_price=D("61000"))
    assert fill.quantity == D("0.004")
    assert fill.fill_price == D("61234.5")
    assert fill.symbol == "XBTUSD"
    assert fill.side is Side.BUY


def test_an_accepted_but_unexecuted_order_raises_the_ports_own_error_with_the_txid():
    a = adapter(
        stubs={
            "AddOrder": ok({"txid": ["OPEN-9"]}),
            "QueryOrders": ok({"OPEN-9": {"status": "open", "vol_exec": "0", "price": "0"}}),
        }
    )
    with pytest.raises(KrakenOrderNotConfirmedError) as err:
        a.submit_order(buy(), market_price=D("61000"))
    # The reconciler catches the PORT's type, so no Kraken-specific branch
    # is needed anywhere upstream.
    assert isinstance(err.value, OrderNotConfirmedError)
    assert err.value.order_id == "OPEN-9"


def test_an_execution_at_a_non_positive_price_is_refused_rather_than_recorded():
    a = adapter(
        stubs={
            "AddOrder": ok({"txid": ["BAD-1"]}),
            "QueryOrders": ok({"BAD-1": {"status": "closed", "vol_exec": "0.01", "price": "0"}}),
        }
    )
    with pytest.raises(KrakenError) as err:
        a.submit_order(buy(), market_price=D("61000"))
    assert "non-positive average price" in str(err.value)


def test_an_order_accepted_with_no_txid_is_an_error():
    a = adapter(stubs={"AddOrder": ok({"descr": {}})})
    with pytest.raises(KrakenError) as err:
        a.submit_order(buy(), market_price=D("61000"))
    assert "no txid" in str(err.value)


def test_the_symbol_is_sent_verbatim_and_never_translated():
    # Kraken names one market three ways. Translating here is how two
    # different instruments eventually become one (D109).
    client = StubClient(
        Balance=BALANCE,
        AddOrder=ok({"txid": ["T1"]}),
        QueryOrders=ok({"T1": {"status": "closed", "vol_exec": "1", "price": "10"}}),
    )
    a = KrakenAdapter(client, validate_only=False)
    a.submit_order(buy(symbol="XBT/USD", quantity=D("1")), market_price=D("10"))
    sent = next(d for p, d in client.calls if p.endswith("AddOrder"))
    assert sent["pair"] == "XBT/USD"


def test_a_limit_order_carries_its_price_and_a_market_order_carries_none():
    client = StubClient(
        Balance=BALANCE,
        AddOrder=ok({"txid": ["T1"]}),
        QueryOrders=ok({"T1": {"status": "closed", "vol_exec": "1", "price": "10"}}),
    )
    a = KrakenAdapter(client, validate_only=False)

    a.submit_order(buy(quantity=D("1")), market_price=D("10"))
    market = next(d for p, d in client.calls if p.endswith("AddOrder"))
    assert market["ordertype"] == "market"
    assert "price" not in market

    client.calls.clear()
    a.submit_order(
        buy(quantity=D("1"), order_type="limit", limit_price=D("9.5")), market_price=D("10")
    )
    limit = next(d for p, d in client.calls if p.endswith("AddOrder"))
    assert limit["ordertype"] == "limit" and limit["price"] == "9.5"


def test_a_limit_order_without_a_price_is_refused_before_it_is_sent():
    a = adapter(stubs={"AddOrder": ok({"txid": ["T"]})})
    with pytest.raises(KrakenError):
        a.submit_order(buy(order_type="limit"), market_price=D("10"))


def test_a_non_positive_quantity_never_reaches_the_venue():
    client = StubClient(Balance=BALANCE)
    a = KrakenAdapter(client, validate_only=False)
    with pytest.raises(KrakenError):
        a.submit_order(buy(quantity=D("0")), market_price=D("10"))
    assert not any(p.endswith("AddOrder") for p, _ in client.calls)


# --- validate-only ----------------------------------------------------------


def test_validate_only_sends_the_flag_and_refuses_to_report_a_fill():
    # There is no Kraken spot sandbox, so this is the rehearsal. Returning
    # a Fill here would fabricate the one thing this module exists not to.
    client = StubClient(Balance=BALANCE, AddOrder=ok({"descr": {"order": "buy 0.01 XBTUSD"}}))
    a = KrakenAdapter(client, validate_only=True)
    with pytest.raises(KrakenError) as err:
        a.submit_order(buy(), market_price=D("61000"))
    assert "VALIDATE_ONLY" in str(err.value)
    sent = next(d for p, d in client.calls if p.endswith("AddOrder"))
    assert sent["validate"] == "true"


# --- cancel -----------------------------------------------------------------


def test_cancelling_nothing_is_a_real_answer_not_an_error():
    a = adapter(stubs={"CancelOrder": ok({"count": 0})})
    assert a.cancel_order("GONE") is False
    a2 = adapter(stubs={"CancelOrder": ok({"count": 1})})
    assert a2.cancel_order("LIVE") is True


# --- signing ----------------------------------------------------------------


def test_the_signature_matches_krakens_published_scheme():
    # Recomputed independently here rather than asserting a golden string:
    # a wrong signature and a wrong key are indistinguishable at the venue,
    # so the test has to encode the algorithm, not a value.
    path = "/0/private/AddOrder"
    data = {"nonce": "1700000000000000000", "pair": "XBTUSD", "type": "buy"}

    post = urllib.parse.urlencode(data)
    expected = base64.b64encode(
        hmac.new(
            base64.b64decode(SECRET),
            path.encode() + hashlib.sha256((data["nonce"] + post).encode()).digest(),
            hashlib.sha512,
        ).digest()
    ).decode()

    assert kraken_signature(path, data, SECRET) == expected


def test_every_request_carries_a_fresh_increasing_nonce():
    # Kraken rejects a nonce at or below the last one this key used.
    client = StubClient(
        Balance=BALANCE,
        AddOrder=ok({"txid": ["T1"]}),
        QueryOrders=ok({"T1": {"status": "closed", "vol_exec": "1", "price": "10"}}),
    )
    a = KrakenAdapter(client, validate_only=False)
    a.submit_order(buy(quantity=D("1")), market_price=D("10"))
    nonces = [int(d["nonce"]) for _, d in client.calls]
    assert len(nonces) >= 2
    assert nonces == sorted(nonces)
    assert len(set(nonces)) == len(nonces)
