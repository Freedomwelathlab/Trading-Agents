"""Signal-driven options bot (Phase 107, D134).

A bot created with structure `signal` does not open the same structure
every day. Each entry decision first scans the UNDERLYING with every chart
setup (the same scan board the dashboards show), and the recommendation
picks a defined-risk credit spread on its side:

* BUY  (a long setup qualifies)  -> bull put spread, sold below the market
* SELL (a short setup qualifies) -> bear call spread, sold above the market
* WAIT                            -> no entry, and the run says why

Credit spreads because they are the structure the options research
modelled (D119: short put spreads +0.10R, not yet an edge) and because they
profit from the direction OR from time, so a signal that stalls is not
automatically a loss. Defined risk either way: max loss = width - credit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.backtesting.brackets import BracketPlan
from apps.api.app.backtesting.setups import SETUPS
from apps.api.app.bots.scanboard import SymbolBoard, build_board
from apps.api.app.marketdata.store import MarketDataStore
from apps.api.app.marketdata.structure import Direction
from apps.api.app.options.paper_orders import OptionStructureType

SIGNAL_MODE = "signal"
SIGNAL_BAR_INTERVAL = "5m"
DEFAULT_MIN_SIGNAL_SCORE = 5


def structure_for(recommendation: str) -> OptionStructureType | None:
    return {
        "BUY": OptionStructureType.BULL_PUT,
        "SELL": OptionStructureType.BEAR_CALL,
    }.get(recommendation)


async def underlying_board(
    session: AsyncSession,
    underlying: str,
    *,
    min_score: int,
    bar_router: Any | None,
    now: datetime | None = None,
    calibration: dict | None = None,
) -> SymbolBoard:
    """The underlying's scan board on the regular session, long and short.
    Refreshes the last few days of 5m bars first when a vendor is wired."""
    store = MarketDataStore(session)
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    if bar_router is not None:
        try:
            fresh = await bar_router.get_bars(
                underlying, bar_interval=SIGNAL_BAR_INTERVAL,
                start_date=today - timedelta(days=4), end_date=today,
            )
            if fresh:
                await store.upsert_bars(fresh)
        except Exception:  # noqa: BLE001 - a vendor failure leaves stored bars as they are
            pass
    bars = await store.get_bars(
        underlying, bar_interval=SIGNAL_BAR_INTERVAL,
        start_date=today - timedelta(days=6), end_date=today,
    )
    return build_board(
        underlying, bars, enabled_setups=sorted(SETUPS), market_type="regular",
        plan=BracketPlan(), min_score=min_score,
        allow_directions=(Direction.LONG, Direction.SHORT), calibration=calibration,
    )
