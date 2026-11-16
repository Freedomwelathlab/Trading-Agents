"""Yahoo daily history (Phase 108, D135): symbol translation is explicit,
dates are the exchange's own, gaps are dropped not filled, and a vendor
refusal is never an empty success."""

from datetime import UTC, date, datetime

import pytest

from apps.api.app.marketdata.provider import DataUnavailableError
from apps.api.app.marketdata.providers.yahoo import (
    UnsupportedSymbolError,
    YahooDailyProvider,
    yahoo_symbol,
)


@pytest.mark.parametrize(
    ("ours", "theirs"),
    [
        ("SPY.US", "SPY"),
        ("brk.b.us", "BRK-B"),
        ("D05.SG", "D05.SI"),
        ("RELIANCE.IN", "RELIANCE.NS"),
        ("RELIANCE.BSE", "RELIANCE.BO"),
        ("700.HK", "0700.HK"),
        ("BTC-USD", "BTC-USD"),
        ("BTC/USD", "BTC-USD"),
        ("EURUSD.FX", "EURUSD=X"),
        ("^NSEI", "^NSEI"),
    ],
)
def test_symbols_are_translated(ours: str, theirs: str) -> None:
    assert yahoo_symbol(ours) == theirs


@pytest.mark.parametrize("bad", ["", "AAPL", "X.ZZ"])
def test_unknown_conventions_are_refused(bad: str) -> None:
    with pytest.raises(UnsupportedSymbolError):
        yahoo_symbol(bad)


def _stamp(y: int, m: int, d: int, h: int = 0) -> int:
    return int(datetime(y, m, d, h, tzinfo=UTC).timestamp())


def _result(stamps, closes, adj=None, offset=0, currency="USD"):
    return {
        "timestamp": stamps,
        "meta": {"gmtoffset": offset, "currency": currency},
        "indicators": {
            "quote": [
                {
                    "close": closes,
                    "open": closes,
                    "high": closes,
                    "low": closes,
                    "volume": [100] * len(closes),
                }
            ],
            "adjclose": [{"adjclose": adj or closes}],
        },
    }


class Stub(YahooDailyProvider):
    def __init__(self, result) -> None:
        super().__init__()
        self._result = result

    async def _chart(self, vendor_symbol, start, end):
        return self._result


@pytest.mark.asyncio
async def test_history_uses_the_exchange_calendar_day_and_drops_gaps() -> None:
    # 01:00 UTC is 09:00 in Singapore the same day. A row with no close is a
    # vendor gap and is dropped, never filled.
    stamps = [_stamp(2026, 11, 12, 1), _stamp(2026, 11, 13, 1), _stamp(2026, 11, 15, 17)]
    result = _result(
        stamps, [10.0, None, 12.0], adj=[9.5, None, 12.0], offset=8 * 3600, currency="SGD"
    )
    h = await Stub(result).get_daily_history(
        "D05.SG", start=date(2026, 11, 1), end=date(2026, 11, 30)
    )
    # 17:00 UTC on the 15th is already the 16th in Singapore.
    assert h.dates == (date(2026, 11, 12), date(2026, 11, 16))
    assert h.closes == (10.0, 12.0) and h.adj_closes == (9.5, 12.0)
    assert h.currency == "SGD" and h.vendor_symbol == "D05.SI" and h.source == "yahoo"


@pytest.mark.asyncio
async def test_no_closes_is_data_unavailable_not_an_empty_history() -> None:
    with pytest.raises(DataUnavailableError):
        await Stub(_result([], [])).get_daily_history(
            "SPY.US", start=date(2026, 1, 1), end=date(2026, 2, 1)
        )


@pytest.mark.asyncio
async def test_bars_are_daily_only_and_carry_their_source() -> None:
    stub = Stub(_result([_stamp(2026, 11, 13, 14)], [754.37]))
    bars = await stub.get_bars(
        "SPY.US", bar_interval="1d", start_date=date(2026, 11, 1), end_date=date(2026, 11, 30)
    )
    assert len(bars) == 1 and bars[0].source == "yahoo" and bars[0].volume == 100
    with pytest.raises(ValueError, match="daily"):
        await stub.get_bars(
            "SPY.US", bar_interval="5m", start_date=date(2026, 11, 1), end_date=date(2026, 11, 2)
        )
