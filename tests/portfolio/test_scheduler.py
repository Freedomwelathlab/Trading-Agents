"""Unit tests for the scheduled-snapshot machinery that needs no database:
mark resolution through a real MarketDataRouter (fed by a fake provider at
the vendor boundary, exactly as tests/marketdata/test_router.py does), and
the interval guards.

The DB-backed half - which brokers are eligible, what is actually written,
and what is deliberately NOT written when a mark is missing - lives in
tests/api/test_snapshot_scheduler.py against real Postgres.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.core.config import Settings
from apps.api.app.marketdata.models import MarketSnapshot
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.router import MarketDataRouter
from apps.api.app.portfolio.scheduler import (
    PortfolioSnapshotScheduler,
    ScheduledSnapshotOutcome,
    ScheduledSnapshotStatus,
    SnapshotCycleResult,
    resolve_marks,
)

NOW = datetime(2026, 8, 29, 15, 0, 0, tzinfo=UTC)


class FakeSymbolProvider:
    """Test double at the vendor boundary only - the router, the scheduler,
    and every price path above them are the real code. Maps symbol ->
    price, or symbol -> exception for the failure cases."""

    name = "fake-symbol-provider"

    def __init__(self, prices: dict[str, Decimal | Exception]) -> None:
        self._prices = prices
        self.calls: list[str] = []

    async def get_snapshot(self, symbol: str) -> MarketSnapshot:
        self.calls.append(symbol)
        value = self._prices.get(symbol)
        if value is None:
            raise DataUnavailableError(f"no such symbol {symbol!r}")
        if isinstance(value, Exception):
            raise value
        return MarketSnapshot(symbol=symbol, price=value, as_of=NOW, source=self.name)


@pytest.mark.asyncio
async def test_resolve_marks_returns_real_prices_for_every_symbol():
    router = MarketDataRouter(
        [FakeSymbolProvider({"AAPL.US": Decimal("190.25"), "MSFT.US": Decimal("410.50")})]
    )

    marks, failures = await resolve_marks(["AAPL.US", "MSFT.US"], router)

    assert marks == {"AAPL.US": Decimal("190.25"), "MSFT.US": Decimal("410.50")}
    assert failures == {}


@pytest.mark.asyncio
async def test_resolve_marks_reports_an_unavailable_symbol_instead_of_substituting_a_price():
    router = MarketDataRouter(
        [
            FakeSymbolProvider(
                {"AAPL.US": Decimal("190.25"), "DELISTED.US": DataUnavailableError("gone")}
            )
        ]
    )

    marks, failures = await resolve_marks(["AAPL.US", "DELISTED.US"], router)

    assert marks == {"AAPL.US": Decimal("190.25")}
    # The failed symbol must be absent from marks entirely - not zero, not
    # carried over from the succeeding symbol, not a stale placeholder.
    assert "DELISTED.US" not in marks
    assert set(failures) == {"DELISTED.US"}
    assert failures["DELISTED.US"].startswith("NO_DATA_AVAILABLE:")


@pytest.mark.asyncio
async def test_resolve_marks_treats_a_broken_vendor_the_same_as_missing_data():
    """A VendorError is a different cause than DataUnavailableError, but the
    consequence for a snapshot is identical: there is no real price, so
    there is no mark."""
    router = MarketDataRouter([FakeSymbolProvider({"AAPL.US": VendorError("rate limited")})])

    marks, failures = await resolve_marks(["AAPL.US"], router)

    assert marks == {}
    assert "rate limited" in failures["AAPL.US"]


@pytest.mark.asyncio
async def test_resolve_marks_with_no_symbols_makes_no_vendor_calls():
    provider = FakeSymbolProvider({})
    marks, failures = await resolve_marks([], MarketDataRouter([provider]))

    assert (marks, failures) == ({}, {})
    assert provider.calls == []


def test_scheduler_refuses_a_non_positive_interval():
    for interval in (0, -1):
        with pytest.raises(ValueError, match="must be positive"):
            PortfolioSnapshotScheduler(
                None,  # type: ignore[arg-type] - rejected before it is ever used
                market_data_router=None,
                interval_seconds=interval,
            )


def test_settings_reject_a_non_positive_interval_at_config_load():
    with pytest.raises(ValueError, match="must be positive"):
        Settings(jwt_secret_key="x" * 32, portfolio_snapshot_interval_seconds=0)


def test_the_scheduler_is_disabled_by_default():
    """The fail-closed default the whole phase rests on (D030) - if this
    ever flips silently, an unattended process starts writing broker
    history on a timer in every deployment that never asked for it."""
    settings = Settings(jwt_secret_key="x" * 32)

    assert settings.portfolio_snapshot_scheduler_enabled is False
    assert settings.portfolio_snapshot_interval_seconds == 3600


def test_cycle_result_counts_captured_and_skipped_separately():
    import uuid

    result = SnapshotCycleResult(
        outcomes=(
            ScheduledSnapshotOutcome(uuid.uuid4(), ScheduledSnapshotStatus.CAPTURED),
            ScheduledSnapshotOutcome(
                uuid.uuid4(), ScheduledSnapshotStatus.SKIPPED_MARKET_DATA_UNAVAILABLE
            ),
            ScheduledSnapshotOutcome(
                uuid.uuid4(), ScheduledSnapshotStatus.SKIPPED_NO_BROKER_ACCOUNT
            ),
        )
    )

    assert result.captured_count == 1
    assert result.skipped_count == 2
