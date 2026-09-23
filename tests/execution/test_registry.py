"""The broker capability registry (Phase 90, D109).

Two kinds of assertion here. The first is that a refusal is SPECIFIC —
naming the venue and the capability — because the whole reason to check
capability before building an order is that the platform already knows the
answer and a numeric vendor code does not convey it. The second is that a
catalogued provider can never be mistaken for a reachable one.
"""

from decimal import Decimal

import pytest

from apps.api.app.execution.broker import OrderRequest
from apps.api.app.execution.registry import (
    PROVIDERS,
    AdapterNotImplementedError,
    AssetClass,
    BrokerCannotError,
    UnknownProviderError,
    build_adapter,
    check_supported,
    get_provider,
    opens_short,
)
from apps.api.app.risk.models import Side

D = Decimal


def order(**over) -> OrderRequest:
    base = dict(symbol="X", side=Side.BUY, quantity=D("1"))
    base.update(over)
    return OrderRequest(**base)  # type: ignore[arg-type]


def test_every_catalogued_provider_is_internally_coherent():
    for name, p in PROVIDERS.items():
        assert p.provider == name
        assert p.asset_classes, f"{name} trades nothing"
        assert p.order_types, f"{name} takes no order type"
        assert p.notes.strip(), f"{name} has no note saying what it really is"
        # A secret field must never be the only identifier of an account:
        # an operator has to be able to confirm WHICH account is wired.
        if p.credential_fields:
            assert any(f.kind.value == "public" for f in p.credential_fields), name


def test_an_unknown_provider_is_an_error_not_a_permissive_default():
    with pytest.raises(UnknownProviderError):
        get_provider("definitely-not-a-broker")


def test_a_catalogued_provider_refuses_to_build_rather_than_returning_a_stand_in():
    # A stand-in would accept orders nothing would ever execute, which is
    # strictly worse than a failure at construction.
    assert get_provider("kraken").adapter_status == "catalogued"
    with pytest.raises(AdapterNotImplementedError) as err:
        build_adapter("kraken", {"api_key": "k", "api_secret": "s"})
    assert "catalogued" in str(err.value)


def test_an_asset_class_the_venue_does_not_trade_is_named_in_the_refusal():
    with pytest.raises(BrokerCannotError) as err:
        check_supported("kraken", asset_class=AssetClass.OPTION)
    assert "Kraken" in str(err.value)
    assert "crypto" in str(err.value)
    assert "option" in str(err.value)


def test_longbridge_is_refused_forex_and_allowed_options():
    with pytest.raises(BrokerCannotError):
        check_supported("longbridge", asset_class=AssetClass.FOREX)
    check_supported("longbridge", asset_class=AssetClass.OPTION)


def test_ibkr_is_the_venue_that_covers_forex_and_futures():
    for cls in (AssetClass.FOREX, AssetClass.FUTURE, AssetClass.EQUITY, AssetClass.OPTION):
        check_supported("ibkr", asset_class=cls)


def test_a_short_is_refused_where_the_venue_cannot_hold_one():
    check_supported("kraken", asset_class=AssetClass.CRYPTO)
    with pytest.raises(BrokerCannotError) as err:
        check_supported("kraken", asset_class=AssetClass.CRYPTO, opens_short=True)
    assert "short" in str(err.value)


def test_extended_hours_is_refused_where_the_venue_has_no_such_session():
    # IG's instruments are CFDs quoted in its own hours; asking for an
    # extended-hours equity session there is a category error.
    with pytest.raises(BrokerCannotError):
        check_supported("ig", asset_class=AssetClass.EQUITY, extended_hours=True)


def test_a_fractional_quantity_is_refused_where_the_venue_deals_in_whole_units():
    check_supported("kraken", asset_class=AssetClass.CRYPTO, order=order(quantity=D("0.25")))
    with pytest.raises(BrokerCannotError) as err:
        check_supported(
            "longbridge", asset_class=AssetClass.EQUITY, order=order(quantity=D("0.25"))
        )
    assert "fractional" in str(err.value)


def test_selling_what_is_held_is_not_a_short():
    # The check that matters: conflating an exit with a short would block
    # every close on a venue that cannot short.
    assert opens_short(Side.SELL, held=D("10"), quantity=D("10")) is False
    assert opens_short(Side.SELL, held=D("10"), quantity=D("12")) is True
    assert opens_short(Side.SELL, held=D("0"), quantity=D("1")) is True
    # A negative holding is already short; selling more extends it.
    assert opens_short(Side.SELL, held=D("-5"), quantity=D("1")) is True
    assert opens_short(Side.BUY, held=D("0"), quantity=D("100")) is False
