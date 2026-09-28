"""Option chain snapshots over HTTP (Phase 100, D119).

Two routers, split by who may use them:

* `POST /admin/options/snapshots/{underlying}` - capture one now. Gated on
  `admin:manage`, because it WRITES ~900 rows to a metered database and
  replaces any snapshot already stored for the vendor's trading day. It
  reads the delayed chain the app is wired with; it never trades.
* `GET /options/snapshots/{underlying}?date=` and `.../dates` - read what
  has been recorded. Any authenticated user: this is market data, exactly
  as the live chain route is.

Every row returned is a stored VENDOR quote (`source` says whose, normally
`cboe-delayed`, ~15 minutes late), not a model. A field the vendor did not
send is null. A date with no snapshot is 404 DATA_UNAVAILABLE - never the
nearest day's chain passed off as the requested one.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.dependencies import get_option_chain_provider
from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.db.base import get_session
from apps.api.app.db.models import User
from apps.api.app.marketdata.option_chain_provider import OptionChainProvider
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.options.snapshots import (
    capture_chain_snapshot,
    list_snapshot_dates,
    load_snapshot,
)

admin_router = APIRouter(
    prefix="/admin/options",
    tags=["admin", "options"],
    dependencies=[Depends(require_permission(Permission.ADMIN))],
)
router = APIRouter(prefix="/options", tags=["options"])

SNAPSHOT_NOTE = (
    "Stored vendor quotes (delayed ~15 minutes when the source is cboe-delayed), filtered to "
    "expiries within the configured DTE and strikes within the configured band of spot. "
    "Greeks and IV are the vendor's. A null is a field the vendor did not send."
)


class SnapshotCaptureResponse(BaseModel):
    underlying: str
    status: str
    trade_date: date
    as_of: datetime
    captured_at: datetime
    source: str
    underlying_price: Decimal
    spot_source: str
    rows_written: int
    rows_replaced: int
    expiries: list[date]
    expiries_unavailable: list[date]


class SnapshotRow(BaseModel):
    contract_symbol: str
    expiry: date
    right: str
    strike: Decimal
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    iv: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    volume: int | None
    open_interest: int | None


class SnapshotResponse(BaseModel):
    underlying: str
    trade_date: date
    as_of: datetime
    captured_at: datetime
    source: str
    underlying_price: Decimal | None
    contracts: int
    rows: list[SnapshotRow]
    note: str = SNAPSHOT_NOTE


class SnapshotDatesResponse(BaseModel):
    underlying: str
    dates: list[date]


@admin_router.post("/snapshots/{underlying}", response_model=SnapshotCaptureResponse)
async def capture_snapshot(
    underlying: str,
    provider: OptionChainProvider | None = Depends(get_option_chain_provider),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SnapshotCaptureResponse:
    """Capture and store one filtered snapshot now, replacing any stored
    for the same vendor trading day. Not restricted to after the close -
    that is the scheduler's rule - so an intraday capture is allowed and
    is later superseded by the scheduled after-close one."""
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="NOT_CONFIGURED: no option chain vendor is wired.",
        )
    try:
        result = await capture_chain_snapshot(
            session,
            provider,
            underlying.upper(),
            max_dte=settings.option_snapshot_max_dte,
            strike_band=settings.option_snapshot_strike_band_pct,
        )
    except DataUnavailableError as exc:
        raise HTTPException(status_code=404, detail=f"DATA_UNAVAILABLE: {exc}") from exc
    except VendorError as exc:
        raise HTTPException(status_code=502, detail=f"VENDOR_ERROR: {exc}") from exc
    await session.commit()
    return SnapshotCaptureResponse(
        underlying=result.underlying,
        status=result.status.value,
        trade_date=result.trade_date,
        as_of=result.as_of,
        captured_at=result.captured_at,
        source=result.source,
        underlying_price=result.underlying_price,
        spot_source=result.spot_source,
        rows_written=result.rows_written,
        rows_replaced=result.rows_replaced,
        expiries=list(result.expiries),
        expiries_unavailable=list(result.expiries_unavailable),
    )


@router.get("/snapshots/{underlying}/dates", response_model=SnapshotDatesResponse)
async def get_snapshot_dates(
    underlying: str,
    session: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
) -> SnapshotDatesResponse:
    """Trade dates with a stored snapshot. Empty is a real answer: nothing
    has been recorded yet (the scheduler is off by default)."""
    symbol = underlying.upper()
    return SnapshotDatesResponse(
        underlying=symbol, dates=await list_snapshot_dates(session, symbol)
    )


@router.get("/snapshots/{underlying}", response_model=SnapshotResponse)
async def get_snapshot(
    underlying: str,
    on: date | None = Query(
        None, alias="date", description="Trade date YYYY-MM-DD; the latest when omitted"
    ),
    session: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
) -> SnapshotResponse:
    symbol = underlying.upper()
    rows = await load_snapshot(session, symbol, on)
    if not rows:
        what = on.isoformat() if on is not None else "any date"
        raise HTTPException(
            status_code=404,
            detail=f"DATA_UNAVAILABLE: no {symbol} option snapshot stored for {what}.",
        )
    first = rows[0]
    return SnapshotResponse(
        underlying=symbol,
        trade_date=first.trade_date,
        as_of=max(r.as_of for r in rows),
        captured_at=max(r.captured_at for r in rows),
        source=first.source,
        underlying_price=first.underlying_price,
        contracts=len(rows),
        rows=[
            SnapshotRow(
                contract_symbol=r.contract_symbol,
                expiry=r.expiry,
                right=r.right,
                strike=r.strike,
                bid=r.bid,
                ask=r.ask,
                last=r.last,
                iv=r.iv,
                delta=r.delta,
                gamma=r.gamma,
                theta=r.theta,
                vega=r.vega,
                volume=r.volume,
                open_interest=r.open_interest,
            )
            for r in rows
        ],
    )
