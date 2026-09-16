"""Unit tests against a fake history-candlestick client - never touches the
real longport SDK, real credentials, or a network call. Mirrors
test_longbridge_history.py's shape.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from apps.api.app.core.config import Settings
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.providers.longbridge import (
    LongbridgeBarBackfillProvider,
    build_longbridge_bar_backfill_provider,
)


class FakeHistoryCandle:
    def __init__(self, *, open, high, low, close, volume, timestamp):
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.timestamp = timestamp


class FakeHistoryCandlestickClient:
    """Stands in for the SDK's paging contract, not just its return value.

    The real `history_candlesticks_by_offset(..., forward=False, count,
    time=anchor, ...)` answers with at most `count` candles STRICTLY OLDER
    than `anchor`, newest-first-relative to that anchor. Reproducing that
    here - rather than handing back the same list every call - is what lets
    these tests catch a paging loop that never advances or that overshoots
    the requested window.
    """

    def __init__(self, *, results=None, error: Exception | None = None, page_size=None):
        self._results = list(results or [])
        self._error = error
        self._page_size = page_size
        self.calls: list[tuple] = []

    async def history_candlesticks_by_offset(
        self, symbol, period, adjust_type, forward, count, time, trade_sessions
    ):
        self.calls.append((symbol, period, adjust_type, forward, count, time, trade_sessions))
        if self._error is not None:
            raise self._error
        limit = self._page_size or count
        older = [c for c in self._results if time is None or c.timestamp < time]
        older.sort(key=lambda c: c.timestamp, reverse=True)
        return list(reversed(older[:limit]))


def make_settings(**overrides) -> Settings:
    defaults = dict(_env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256")
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_returns_bars_oldest_first_regardless_of_sdk_order():
    # Deliberately out of order - the provider must sort by timestamp
    # itself, never trust the SDK's return order.
    candles = [
        FakeHistoryCandle(
            open="101", high="103", low="100", close="102", volume=1_000,
            timestamp=datetime(2026, 8, 25, tzinfo=UTC),
        ),
        FakeHistoryCandle(
            open="99", high="101", low="98", close="100", volume=900,
            timestamp=datetime(2026, 8, 23, tzinfo=UTC),
        ),
    ]
    client = FakeHistoryCandlestickClient(results=candles)
    provider = LongbridgeBarBackfillProvider(client)

    bars = await provider.get_bars(
        "AAPL.US", bar_interval="1d", start_date=date(2026, 8, 23), end_date=date(2026, 8, 25)
    )

    assert [bar.close for bar in bars] == [Decimal("100"), Decimal("102")]
    assert bars[0].symbol == "AAPL.US"
    assert bars[0].bar_interval == "1d"
    assert bars[0].source == "longbridge"
    assert bars[0].open == Decimal("99")
    assert bars[0].high == Decimal("101")
    assert bars[0].low == Decimal("98")
    assert bars[0].volume == 900


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bar_interval", "expected_period"),
    [
        ("1m", "Min_1"),
        ("5m", "Min_5"),
        ("15m", "Min_15"),
        ("30m", "Min_30"),
        ("1h", "Min_60"),
        ("1d", "Day"),
    ],
)
async def test_each_interval_asks_the_vendor_for_the_matching_period(
    bar_interval: str, expected_period: str
):
    """Phase 70 (D088). This provider accepted only `1d` until this phase.

    **What this proves and what it does not.** It proves the adapter asks
    the SDK for the right `Period` member for each interval in the
    `BarInterval` vocabulary - which is the part of the mapping this
    codebase owns, and the part where a mistake would be invisible
    (persisting daily bars under a `bar_interval` of `"5m"`). It does NOT
    prove Longbridge returns intraday candles for any given symbol or
    entitlement: that needs a live call with a valid token, and the token
    available when this was written was expired. See D088 section 4.
    """
    candle = FakeHistoryCandle(
        open="99", high="101", low="98", close="100", volume=900,
        timestamp=datetime(2026, 8, 23, 14, 30, tzinfo=UTC),
    )
    client = FakeHistoryCandlestickClient(results=[candle])
    provider = LongbridgeBarBackfillProvider(client)

    bars = await provider.get_bars(
        "AAPL.US",
        bar_interval=bar_interval,
        start_date=date(2026, 8, 23),
        end_date=date(2026, 8, 25),
    )

    # The period the SDK was actually handed, compared by NAME rather than
    # by importing `Period` here - the vendor package stays out of this
    # test file exactly as it stays out of the provider's module scope.
    (_symbol, period, _adjust, _forward, _count, _time, _sessions) = client.calls[0]
    assert getattr(period, "name", str(period)).endswith(expected_period)

    # And the bar is tagged with OUR interval, not the vendor's spelling -
    # this is the string every later reader matches on exactly.
    assert bars[0].bar_interval == bar_interval


@pytest.mark.asyncio
async def test_an_unmappable_interval_raises_rather_than_falling_back_to_daily():
    """The failure mode this refusal exists to prevent is silent and
    undetectable: falling back to `Period.Day` would persist DAILY bars
    under whatever `bar_interval` was requested, and every later reader -
    backtest, deployment, chart - would treat them as that interval's data.
    Nothing downstream could tell."""
    provider = LongbridgeBarBackfillProvider(FakeHistoryCandlestickClient(results=[]))

    with pytest.raises(ValueError, match="cannot map bar_interval"):
        await provider.get_bars(
            "AAPL.US", bar_interval="1w", start_date=date(2026, 8, 1), end_date=date(2026, 8, 2)
        )


@pytest.mark.asyncio
async def test_empty_results_is_data_unavailable():
    client = FakeHistoryCandlestickClient(results=[])
    provider = LongbridgeBarBackfillProvider(client)

    with pytest.raises(DataUnavailableError):
        await provider.get_bars(
            "NOSUCH.US", bar_interval="1d", start_date=date(2026, 8, 1), end_date=date(2026, 8, 2)
        )


@pytest.mark.asyncio
async def test_an_sdk_exception_becomes_a_typed_vendor_error():
    client = FakeHistoryCandlestickClient(error=RuntimeError("rate limited"))
    provider = LongbridgeBarBackfillProvider(client)

    with pytest.raises(VendorError, match="rate limited"):
        await provider.get_bars(
            "AAPL.US", bar_interval="1d", start_date=date(2026, 8, 1), end_date=date(2026, 8, 2)
        )


def test_build_returns_none_when_no_credentials_are_configured():
    settings = make_settings()
    assert build_longbridge_bar_backfill_provider(settings) is None


def test_build_returns_none_when_only_some_credentials_are_configured():
    settings = make_settings(longport_app_key="key", longport_app_secret="secret")
    assert build_longbridge_bar_backfill_provider(settings) is None


@pytest.mark.asyncio
async def test_pages_backward_until_the_window_start_is_reached():
    """Phase 73 (D091) - the regression that motivated this whole change.

    The vendor caps a response at 1,000 candles and serves the most RECENT
    ones, ignoring how far back the caller asked for. A single request for
    a 5-minute window longer than ~13 trading days therefore returned a
    short series with no error and no truncation flag.

    Sixty bars behind a page size of 25 forces three round trips. The
    assertion that matters is not "three calls" but that the OLDEST bar in
    the window comes back: a provider that pages once, or that loops on an
    anchor it never advances, fails on the first bar rather than on a count.
    """
    candles = [
        FakeHistoryCandle(
            open="10", high="11", low="9", close=str(100 + i), volume=5,
            timestamp=datetime(2026, 8, 3, 13, 30, tzinfo=UTC) + timedelta(minutes=5 * i),
        )
        for i in range(60)
    ]
    client = FakeHistoryCandlestickClient(results=candles, page_size=25)
    provider = LongbridgeBarBackfillProvider(client)

    bars = await provider.get_bars(
        "TQQQ.US", bar_interval="5m", start_date=date(2026, 8, 3), end_date=date(2026, 8, 3)
    )

    assert len(bars) == 60
    assert bars[0].ts == datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
    assert bars[-1].ts == datetime(2026, 8, 3, 18, 25, tzinfo=UTC)
    assert [b.close for b in bars] == [Decimal(str(100 + i)) for i in range(60)]
    assert len(client.calls) >= 3

    # Each page must ask for something strictly older than the last, or the
    # loop is not paging at all.
    anchors = [call[5] for call in client.calls]
    assert anchors == sorted(anchors, reverse=True)
    assert len(set(anchors)) == len(anchors)


@pytest.mark.asyncio
async def test_candles_outside_the_requested_window_are_dropped():
    """Paging backward overshoots by up to a full page, and the vendor also
    returns candles outside a requested range unprompted. Without the
    filter the caller's window silently widens - and a backtest told to run
    one month would quietly replay two."""
    inside = FakeHistoryCandle(
        open="1", high="2", low="1", close="10", volume=1,
        timestamp=datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
    )
    too_old = FakeHistoryCandle(
        open="1", high="2", low="1", close="99", volume=1,
        timestamp=datetime(2026, 7, 1, 15, 0, tzinfo=UTC),
    )
    too_new = FakeHistoryCandle(
        open="1", high="2", low="1", close="98", volume=1,
        timestamp=datetime(2026, 9, 1, 15, 0, tzinfo=UTC),
    )
    client = FakeHistoryCandlestickClient(results=[too_old, inside, too_new])
    provider = LongbridgeBarBackfillProvider(client)

    bars = await provider.get_bars(
        "TQQQ.US", bar_interval="5m", start_date=date(2026, 8, 10), end_date=date(2026, 8, 10)
    )

    assert [b.close for b in bars] == [Decimal("10")]


@pytest.mark.asyncio
async def test_the_whole_of_the_end_date_is_included():
    """The window is half-open at the top - `end_date` + 1 day - so a bar
    late on the final session is kept. Comparing against `end_date` at
    midnight would silently drop every intraday bar of the last day, which
    reads as "the vendor had no data yet" rather than as an off-by-one."""
    late = FakeHistoryCandle(
        open="1", high="2", low="1", close="42", volume=1,
        timestamp=datetime(2026, 8, 10, 23, 55, tzinfo=UTC),
    )
    client = FakeHistoryCandlestickClient(results=[late])
    provider = LongbridgeBarBackfillProvider(client)

    bars = await provider.get_bars(
        "TQQQ.US", bar_interval="5m", start_date=date(2026, 8, 10), end_date=date(2026, 8, 10)
    )

    assert [b.close for b in bars] == [Decimal("42")]


@pytest.mark.asyncio
async def test_intraday_asks_for_the_extended_session_and_daily_does_not():
    """The playbook prices premarket highs/lows, which do not exist inside
    the regular session. Daily bars stay on `Intraday` because widening
    them would change what an existing `1d` bar means for every caller
    already reading them."""
    candle = FakeHistoryCandle(
        open="1", high="2", low="1", close="10", volume=1,
        timestamp=datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
    )

    for interval, expected in (("5m", "All"), ("15m", "All"), ("1d", "Intraday")):
        client = FakeHistoryCandlestickClient(results=[candle])
        provider = LongbridgeBarBackfillProvider(client)
        await provider.get_bars(
            "TQQQ.US",
            bar_interval=interval,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
        )
        sessions = client.calls[0][6]
        assert getattr(sessions, "name", str(sessions)).endswith(expected), interval


def test_a_naive_sdk_timestamp_is_read_as_local_time_not_asserted_to_be_utc():
    """Phase 73 (D091). The SDK converts its epochs to the running
    process's timezone and returns a NAIVE datetime, so the same candle
    reads `13:30` on a UTC host and `21:30` on a UTC+8 one.

    The old `.replace(tzinfo=UTC)` asserted that whatever the local clock
    said was UTC. That is true only on a UTC host - which the API
    container happens to be, which is why it survived. On the UTC+8
    development machine it moved every bar eight hours, putting the US
    regular session after midnight. Daily bars hid it (their timestamps
    are midnight ET, so the date label was unchanged); intraday bars,
    where the session boundary carries the meaning, were corrupted.

    Asserted against a FIXED offset rather than the ambient host zone, so
    the test means the same thing wherever it runs.
    """
    from datetime import timedelta, timezone

    from apps.api.app.marketdata.providers.longbridge import _as_utc

    plus_eight = timezone(timedelta(hours=8))
    # 09:30 ET on 2026-09-15 (EDT) is 13:30Z, which a UTC+8 host renders
    # as the naive wall-clock 21:30.
    aware = datetime(2026, 9, 15, 21, 30, tzinfo=plus_eight)
    assert _as_utc(aware) == datetime(2026, 9, 15, 13, 30, tzinfo=UTC)

    # An aware timestamp is converted by its own offset, never relabelled.
    assert _as_utc(datetime(2026, 9, 15, 13, 30, tzinfo=UTC)) == datetime(
        2026, 9, 15, 13, 30, tzinfo=UTC
    )

    # Epoch seconds are unambiguous and must be unaffected by any of this.
    assert _as_utc(1_757_943_000) == datetime.fromtimestamp(1_757_943_000, tz=UTC)
