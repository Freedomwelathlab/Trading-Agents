"""Unit tests for the LIVE broker adapter (Phase 43, D058).

NO REAL BROKER IS EVER CONTACTED HERE. Every test injects a fake client
that satisfies `LiveTradeClient` and records what it was asked to do -
exactly the pattern D015's Longbridge quote-provider tests use. The real
`longport` SDK is imported only for its enums (`OrderSide`/`OrderType`/
`TimeInForceType`), which is a pure in-process import, never a connection.

`build_live_broker_adapter()` is likewise only ever exercised in its
REFUSING direction: every settings object below has
`live_trading_enabled=False` or a non-live trading mode, so no
`TradeContext` is ever constructed and no credential is ever used.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.core.config import Settings, TradingMode
from apps.api.app.execution.broker import OrderRequest
from apps.api.app.execution.live_broker import (
    LiveBrokerAdapter,
    LiveBrokerError,
    LiveOrderNotFilledError,
    build_live_broker_adapter,
)
from apps.api.app.risk.models import Side


class _Balance:
    def __init__(self, currency: str, total_cash: str) -> None:
        self.currency = currency
        self.total_cash = Decimal(total_cash)


class _Position:
    def __init__(self, symbol: str, quantity: str) -> None:
        self.symbol = symbol
        self.quantity = Decimal(quantity)


class _Channel:
    def __init__(self, positions: list[_Position]) -> None:
        self.positions = positions


class _PositionsResponse:
    def __init__(self, channels: list[_Channel]) -> None:
        self.channels = channels


class _Status:
    def __init__(self, name: str) -> None:
        self.name = name


class _OrderDetail:
    def __init__(
        self,
        *,
        status: str,
        executed_quantity: str | None,
        executed_price: str | None,
        updated_at: datetime | None = None,
    ) -> None:
        self.status = _Status(status)
        self.executed_quantity = None if executed_quantity is None else Decimal(executed_quantity)
        self.executed_price = None if executed_price is None else Decimal(executed_price)
        self.updated_at = updated_at


class _SubmitResponse:
    def __init__(self, order_id: str) -> None:
        self.order_id = order_id


class FakeLiveTradeClient:
    """Stands in for `longport.openapi.TradeContext`. Never opens a socket."""

    def __init__(
        self,
        *,
        balances: list[_Balance] | None = None,
        positions: list[_Position] | None = None,
        detail: _OrderDetail | None = None,
        submit_raises: Exception | None = None,
        detail_raises: Exception | None = None,
        balance_raises: Exception | None = None,
    ) -> None:
        self._balances = balances if balances is not None else [_Balance("USD", "100000")]
        self._positions = positions or []
        self._detail = detail or _OrderDetail(
            status="Filled", executed_quantity="10", executed_price="100"
        )
        self._submit_raises = submit_raises
        self._detail_raises = detail_raises
        self._balance_raises = balance_raises
        self.submitted: list[dict[str, object]] = []
        self.order_detail_calls: list[str] = []

    def submit_order(self, **kwargs: object) -> _SubmitResponse:
        if self._submit_raises is not None:
            raise self._submit_raises
        self.submitted.append(kwargs)
        return _SubmitResponse("LB-ORDER-1")

    def order_detail(self, order_id: str) -> _OrderDetail:
        self.order_detail_calls.append(order_id)
        if self._detail_raises is not None:
            raise self._detail_raises
        return self._detail

    def stock_positions(self, symbols: list[str] | None = None) -> _PositionsResponse:
        return _PositionsResponse([_Channel(self._positions)])

    def account_balance(self, currency: str | None = None) -> list[_Balance]:
        if self._balance_raises is not None:
            raise self._balance_raises
        return self._balances


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"jwt_secret_key": "test-only-not-a-real-secret"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- account state ------------------------------------------------------


def test_account_state_is_built_from_the_brokers_own_cash_and_positions():
    client = FakeLiveTradeClient(
        balances=[_Balance("USD", "50000")], positions=[_Position("AAPL", "10")]
    )
    adapter = LiveBrokerAdapter(client)

    state = adapter.get_account_state(marks={"AAPL": Decimal("200")})

    assert state.cash == Decimal("50000")
    assert state.current_exposure == Decimal("2000")
    assert state.equity == Decimal("52000")


def test_a_missing_mark_for_a_live_position_raises_rather_than_guessing():
    client = FakeLiveTradeClient(positions=[_Position("AAPL", "10")])
    adapter = LiveBrokerAdapter(client)

    with pytest.raises(ValueError, match="No mark supplied"):
        adapter.get_account_state(marks={})


def test_a_missing_balance_in_the_configured_currency_fails_closed():
    client = FakeLiveTradeClient(balances=[_Balance("HKD", "50000")])
    adapter = LiveBrokerAdapter(client, currency="USD")

    with pytest.raises(LiveBrokerError, match="no USD balance"):
        adapter.get_account_state(marks={})


def test_a_broker_error_reading_the_balance_surfaces_as_a_live_broker_error():
    client = FakeLiveTradeClient(balance_raises=RuntimeError("connection reset"))
    adapter = LiveBrokerAdapter(client)

    with pytest.raises(LiveBrokerError, match="Could not read live account balance"):
        adapter.get_account_state(marks={})


def test_the_account_snapshot_is_read_once_per_adapter_not_per_property_access():
    """One trade must reason about ONE book - see the class docstring."""
    calls: list[int] = []

    class CountingClient(FakeLiveTradeClient):
        def account_balance(self, currency: str | None = None) -> list[_Balance]:
            calls.append(1)
            return super().account_balance(currency)

    adapter = LiveBrokerAdapter(CountingClient(positions=[_Position("AAPL", "1")]))
    assert adapter.cash == Decimal("100000")
    assert adapter.positions == {"AAPL": Decimal("1")}
    adapter.get_account_state(marks={"AAPL": Decimal("100")})

    assert len(calls) == 1


def test_positions_returns_a_copy_that_cannot_mutate_the_adapter():
    adapter = LiveBrokerAdapter(FakeLiveTradeClient(positions=[_Position("AAPL", "10")]))
    snapshot = adapter.positions
    snapshot["AAPL"] = Decimal("999")
    assert adapter.positions["AAPL"] == Decimal("10")


# --- order submission ---------------------------------------------------


def test_a_filled_order_returns_the_brokers_own_executed_quantity_and_price():
    filled_at = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    client = FakeLiveTradeClient(
        detail=_OrderDetail(
            status="Filled",
            executed_quantity="10",
            executed_price="101.25",
            updated_at=filled_at,
        )
    )
    adapter = LiveBrokerAdapter(client)

    fill = adapter.submit_order(
        OrderRequest(symbol="AAPL", side=Side.BUY, quantity=Decimal("10")),
        # A deliberately wrong "market price" - if this ever appears in the
        # fill, the adapter is fabricating.
        market_price=Decimal("999999"),
    )

    assert fill.fill_price == Decimal("101.25")
    assert fill.quantity == Decimal("10")
    assert fill.filled_at == filled_at
    assert adapter.fills == [fill]


def test_the_caller_supplied_market_price_is_never_sent_to_the_broker():
    client = FakeLiveTradeClient()
    adapter = LiveBrokerAdapter(client)

    adapter.submit_order(
        OrderRequest(symbol="AAPL", side=Side.BUY, quantity=Decimal("10")),
        market_price=Decimal("123.45"),
    )

    (submitted,) = client.submitted
    assert Decimal("123.45") not in submitted.values()
    assert submitted["symbol"] == "AAPL"
    assert submitted["submitted_quantity"] == Decimal("10")


def test_the_order_is_submitted_as_a_day_market_order_with_the_right_side():
    from longport.openapi import OrderSide, OrderType, TimeInForceType

    client = FakeLiveTradeClient()
    adapter = LiveBrokerAdapter(client)

    adapter.submit_order(
        OrderRequest(symbol="AAPL", side=Side.SELL, quantity=Decimal("3")),
        market_price=Decimal("100"),
    )

    (submitted,) = client.submitted
    assert submitted["side"] == OrderSide.Sell
    assert submitted["order_type"] == OrderType.MO
    assert submitted["time_in_force"] == TimeInForceType.Day


def test_an_unfilled_order_raises_with_the_real_order_id_and_no_fabricated_fill():
    client = FakeLiveTradeClient(
        detail=_OrderDetail(status="New", executed_quantity="0", executed_price=None)
    )
    adapter = LiveBrokerAdapter(client)

    with pytest.raises(LiveOrderNotFilledError) as excinfo:
        adapter.submit_order(
            OrderRequest(symbol="AAPL", side=Side.BUY, quantity=Decimal("10")),
            market_price=Decimal("100"),
        )

    assert excinfo.value.order_id == "LB-ORDER-1"
    assert adapter.fills == []


def test_an_unreadable_order_status_raises_unfilled_carrying_the_order_id():
    client = FakeLiveTradeClient(detail_raises=RuntimeError("timeout"))
    adapter = LiveBrokerAdapter(client)

    with pytest.raises(LiveOrderNotFilledError) as excinfo:
        adapter.submit_order(
            OrderRequest(symbol="AAPL", side=Side.BUY, quantity=Decimal("10")),
            market_price=Decimal("100"),
        )

    assert excinfo.value.order_id == "LB-ORDER-1"


def test_a_submission_failure_raises_and_never_reads_an_order_back():
    client = FakeLiveTradeClient(submit_raises=RuntimeError("rejected by venue"))
    adapter = LiveBrokerAdapter(client)

    with pytest.raises(LiveBrokerError, match="Live order submission failed"):
        adapter.submit_order(
            OrderRequest(symbol="AAPL", side=Side.BUY, quantity=Decimal("10")),
            market_price=Decimal("100"),
        )
    assert client.order_detail_calls == []


def test_a_fractional_quantity_is_refused_rather_than_rounded():
    client = FakeLiveTradeClient()
    adapter = LiveBrokerAdapter(client)

    with pytest.raises(LiveBrokerError, match="fractional"):
        adapter.submit_order(
            OrderRequest(symbol="AAPL", side=Side.BUY, quantity=Decimal("1.5")),
            market_price=Decimal("100"),
        )
    assert client.submitted == []


def test_the_cached_snapshot_is_invalidated_after_a_fill():
    client = FakeLiveTradeClient(
        balances=[_Balance("USD", "1000")], positions=[_Position("AAPL", "1")]
    )
    adapter = LiveBrokerAdapter(client)
    assert adapter.cash == Decimal("1000")

    adapter.submit_order(
        OrderRequest(symbol="AAPL", side=Side.BUY, quantity=Decimal("1")),
        market_price=Decimal("100"),
    )

    client._balances = [_Balance("USD", "900")]
    client._positions = [_Position("AAPL", "2")]
    assert adapter.cash == Decimal("900")
    assert adapter.positions == {"AAPL": Decimal("2")}


# --- construction gate --------------------------------------------------
#
# Every case below asserts that NO adapter is built. There is deliberately
# no test of the accepting direction: building one requires
# LIVE_TRADING_ENABLED=true, which this repository's tests must never set.


def test_no_adapter_is_built_in_research_mode():
    assert build_live_broker_adapter(_settings()) is None


def test_no_adapter_is_built_in_paper_mode_even_with_live_credentials_present():
    settings = _settings(
        trading_mode=TradingMode.PAPER,
        longport_live_app_key="k",
        longport_live_app_secret="s",  # pragma: allowlist secret
        longport_live_access_token="t",
    )
    assert build_live_broker_adapter(settings) is None


def test_no_adapter_is_built_when_live_trading_is_disabled():
    """The default posture. `trading_mode` cannot be LIVE here - Settings
    itself refuses that combination - so this covers the other half: the
    flag is off and nothing is constructed."""
    settings = _settings(
        live_trading_enabled=False,
        longport_live_app_key="k",
        longport_live_app_secret="s",  # pragma: allowlist secret
        longport_live_access_token="t",
    )
    assert build_live_broker_adapter(settings) is None


def test_settings_refuses_to_construct_live_mode_without_the_enable_flag():
    """The fail-closed gate this whole phase sits behind, re-asserted here
    because D058 depends on it: TRADING_MODE=live with
    LIVE_TRADING_ENABLED=false is not a configuration this app will boot
    into at all."""
    with pytest.raises(ValueError, match="LIVE_TRADING_ENABLED=true"):
        _settings(trading_mode=TradingMode.LIVE, live_trading_enabled=False)


@pytest.mark.parametrize(
    "present",
    [
        {"longport_live_app_key": "k"},
        {"longport_live_app_secret": "s"},  # pragma: allowlist secret
        {"longport_live_access_token": "t"},
        {"longport_live_app_key": "k", "longport_live_app_secret": "s"},
    ],
)
def test_a_partial_live_credential_trio_builds_nothing(present: dict[str, str]):
    assert build_live_broker_adapter(_settings(**present)) is None


def test_the_quote_credentials_are_not_reused_as_trading_credentials():
    """Setting the read-only LONGPORT_* trio must not make a live trading
    path appear - they are different credentials for different powers."""
    settings = _settings(
        longport_app_key="k",
        longport_app_secret="s",  # pragma: allowlist secret
        longport_access_token="t",
    )
    assert build_live_broker_adapter(settings) is None
