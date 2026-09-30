"""IG historical prices -> FX mid bars, against a STUBBED IG (Phase 103,
D123). The real IG key is suspended, so nothing here touches the network;
the stub answers in the shape IG's REST documentation describes."""

from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from apps.api.app.execution.adapters.ig import IgAdapter
from apps.api.app.marketdata.bar_router import BarBackfillRouter, UnroutableSymbolError
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.providers.ig_prices import (
    IgFxBarProvider,
    build_ig_fx_bar_provider,
)


def _price(bid, ask):
    return {"bid": bid, "ask": ask, "lastTraded": None}


def _row(ts, o, h, low, c, *, spread=1.0):
    return {
        "snapshotTime": ts.replace("-", "/").replace("T", " "),
        "snapshotTimeUTC": ts,
        "openPrice": _price(o, o + spread),
        "highPrice": _price(h, h + spread),
        "lowPrice": _price(low, low + spread),
        "closePrice": _price(c, c + spread),
        "lastTradedVolume": 812,  # a TICK COUNT on an FX CFD - must not become volume
    }


class StubIg:
    """Records every request; serves /session, /markets and paged /prices."""

    def __init__(self, pages, *, scaling=10000, login_error=None, prices_error=None):
        self.pages = pages
        self.scaling = scaling
        self.login_error = login_error
        self.prices_error = prices_error
        self.calls: list[tuple[str, str, str]] = []

    def request(self, method, path, *, version, body=None, headers=None):
        self.calls.append((method, path, version))
        if path == "/session":
            if self.login_error:
                return 403, {}, {"errorCode": self.login_error}
            return 200, {"CST": "c", "X-SECURITY-TOKEN": "x"}, {}
        if path.startswith("/markets/"):
            snap = {} if self.scaling is None else {"scalingFactor": self.scaling}
            return 200, {}, {"snapshot": snap}
        if path.startswith("/prices/"):
            if self.prices_error:
                return 403, {}, {"errorCode": self.prices_error}
            page = int(parse_qs(urlsplit(path).query)["pageNumber"][0])
            return 200, {}, {
                "prices": self.pages[page - 1],
                "metadata": {
                    "allowance": {"remainingAllowance": 9000, "totalAllowance": 10000},
                    "pageData": {"pageNumber": page, "totalPages": len(self.pages)},
                },
            }
        raise AssertionError(f"unexpected {method} {path}")


def provider(stub):
    adapter = IgAdapter(stub, api_key="k", username="u", password="p", account_type="DEMO")
    return IgFxBarProvider(adapter)


PAGE1 = [
    _row("2025-06-04T08:00:00", 11000.0, 11004.0, 10998.0, 11002.0),
    _row("2025-06-04T08:05:00", 11002.0, 11003.0, 11001.0, 11001.5, spread=0.6),
]
PAGE2 = [_row("2025-06-04T08:10:00", 11001.5, 11001.5, 11000.0, 11000.5)]


def test_bid_ask_become_scaled_mid_bars_with_no_volume_and_a_spread_series():
    stub = StubIg([PAGE1, PAGE2])
    series = provider(stub).fetch(
        "EURUSD.FX", bar_interval="5m", start_date=date(2025, 6, 4), end_date=date(2025, 6, 4)
    )
    assert len(series.bars) == 3  # both pages read
    first = series.bars[0]
    assert first.ts == datetime(2025, 6, 4, 8, 0, tzinfo=UTC)
    assert first.close == Decimal("1.100250")  # (11002 + 11003) / 2 / 10000
    assert first.high == Decimal("1.100450")
    assert first.volume is None
    assert first.source == "ig-mid"
    assert series.spreads[0][1] == Decimal("0.0001")  # 1 point = 1 pip on EUR/USD
    summary = series.spread_pips_summary(Decimal("0.0001"))
    assert summary is not None and summary["median"] == Decimal("1.0")
    assert series.remaining_allowance == 9000
    prices_calls = [c for c in stub.calls if c[1].startswith("/prices/CS.D.EURUSD.MINI.IP")]
    assert len(prices_calls) == 2 and all(v == "3" for _, _, v in prices_calls)
    assert "resolution=MINUTE_5" in prices_calls[0][1]
    assert all(method == "GET" for method, path, _ in stub.calls if path != "/session")


def test_a_row_without_a_two_sided_close_is_skipped_not_filled_from_one_side():
    row = _row("2025-06-04T08:15:00", 11000.0, 11001.0, 10999.0, 11000.0)
    row["closePrice"] = {"bid": 11000.0, "ask": None}
    series = provider(StubIg([[*PAGE1, row]])).fetch(
        "EURUSD.FX", bar_interval="5m", start_date=date(2025, 6, 4), end_date=date(2025, 6, 4)
    )
    assert len(series.bars) == 2 and series.skipped_rows == 1


def test_a_missing_scaling_factor_is_refused_rather_than_guessed():
    with pytest.raises(VendorError, match="scalingFactor"):
        provider(StubIg([PAGE1], scaling=None)).fetch(
            "EURUSD.FX", bar_interval="5m", start_date=date(2025, 6, 4),
            end_date=date(2025, 6, 4),
        )


def test_daily_bars_are_refused():
    with pytest.raises(ValueError, match="Daily FX bars"):
        provider(StubIg([PAGE1])).fetch(
            "EURUSD.FX", bar_interval="1d", start_date=date(2025, 6, 4),
            end_date=date(2025, 6, 4),
        )


def test_an_empty_window_is_data_unavailable():
    with pytest.raises(DataUnavailableError):
        provider(StubIg([[]])).fetch(
            "EURUSD.FX", bar_interval="5m", start_date=date(2025, 6, 7),
            end_date=date(2025, 6, 7),
        )


def test_a_suspended_client_and_an_exhausted_allowance_surface_as_vendor_errors():
    with pytest.raises(VendorError, match="SUSPENDED"):
        provider(StubIg([PAGE1], login_error="error.security.client-suspended")).fetch(
            "EURUSD.FX", bar_interval="5m", start_date=date(2025, 6, 4),
            end_date=date(2025, 6, 4),
        )
    allowance = "error.public-api.exceeded-account-historical-data-allowance"
    with pytest.raises(VendorError, match="historical-data-allowance"):
        provider(StubIg([PAGE1], prices_error=allowance)).fetch(
            "EURUSD.FX", bar_interval="5m", start_date=date(2025, 6, 4),
            end_date=date(2025, 6, 4),
        )


@pytest.mark.asyncio
async def test_the_router_sends_fx_to_the_fx_provider_and_never_to_longbridge():
    fx = provider(StubIg([PAGE1]))

    class Equity:
        name = "longbridge"

        async def get_bars(self, *a, **k):  # pragma: no cover - must not be called
            raise AssertionError("an FX symbol reached the equity vendor")

    router = BarBackfillRouter(equity_provider=Equity(), fx_provider=fx)
    assert router.provider_for("EURUSD.FX") is fx
    assert router.configured_vendors == ("longbridge", "ig")
    bars = await router.get_bars(
        "EURUSD.FX", bar_interval="5m", start_date=date(2025, 6, 4), end_date=date(2025, 6, 4)
    )
    assert len(bars) == 2


def test_an_unconfigured_fx_leg_names_the_setting_to_fix():
    router = BarBackfillRouter(equity_provider=object())  # type: ignore[arg-type]
    with pytest.raises(UnroutableSymbolError, match="FX_BAR_PROVIDER=ig"):
        router.provider_for("EURUSD.FX")


class _Settings:
    def __init__(self, provider="none"):
        self.fx_bar_provider = provider
        self.fx_ig_epic_template = "CS.D.{pair}.MINI.IP"


FULL_IG = {
    "IG_API_KEY": "k", "IG_USERNAME": "u", "IG_PASSWORD": "p", "IG_ACCOUNT_TYPE": "DEMO",
}


def test_builder_is_off_by_default_and_needs_complete_credentials():
    assert build_ig_fx_bar_provider(_Settings("none"), FULL_IG) is None
    assert build_ig_fx_bar_provider(_Settings("ig"), {"IG_API_KEY": "k"}) is None
    built = build_ig_fx_bar_provider(_Settings("ig"), FULL_IG)
    assert isinstance(built, IgFxBarProvider)
