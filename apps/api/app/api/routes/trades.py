"""The only HTTP entrypoint that can move a trade toward the broker. It
always goes through submit_trade_and_record() (apps/api/app/oms/persistence.py),
which itself always goes through the risk engine first - there is no
shortcut from this route to a broker call.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.dependencies import AuthorizedBroker, require_broker_access
from apps.api.app.api.schemas import TradeSubmissionRequest, TradeSubmissionResponse
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.db.base import get_session
from apps.api.app.db.models import BrokerKind, OrderStatus
from apps.api.app.execution.persistence import load_paper_broker, save_paper_broker
from apps.api.app.oms.persistence import submit_trade_and_record
from apps.api.app.risk.models import RiskLimits, TradeProposal

router = APIRouter(prefix="/brokers/{broker_id}/trades", tags=["trades"])


@router.post("", response_model=TradeSubmissionResponse)
async def submit_trade_endpoint(
    broker_id: uuid.UUID,
    request: TradeSubmissionRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    authorized: AuthorizedBroker = Depends(require_broker_access(Permission.SUBMIT_PAPER_TRADE)),
) -> TradeSubmissionResponse:
    if authorized.broker.kind is not BrokerKind.PAPER:
        raise HTTPException(
            status_code=400,
            detail=(
                "NOT_CONFIGURED: this endpoint only supports paper brokers. "
                "Live trading has no implemented execution path (spec §3/§46)."
            ),
        )

    proposal = TradeProposal(
        symbol=request.symbol,
        side=request.side,
        quantity=request.quantity,
        estimated_price=request.estimated_price,
        stop_price=request.stop_price,
        market_data_as_of=request.market_data_as_of or datetime.now(UTC),
    )

    # Locks the broker's account row for the rest of this transaction -
    # see apps/api/app/execution/persistence.py's module docstring for why.
    broker_adapter = await load_paper_broker(
        session, broker_id, default_starting_cash=settings.paper_broker_starting_cash
    )
    marks = {**request.marks, request.symbol: request.estimated_price}
    try:
        account = broker_adapter.get_account_state(marks=marks)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"DATA_UNAVAILABLE: {exc}") from None

    limits = RiskLimits(
        max_position_pct_of_equity=settings.risk_max_position_pct_of_equity,
        max_portfolio_exposure_pct_of_equity=settings.risk_max_portfolio_exposure_pct_of_equity,
        max_risk_pct_of_equity_per_trade=settings.risk_max_risk_pct_of_equity_per_trade,
        require_stop_price=settings.risk_require_stop_price,
        max_market_data_age_seconds=settings.risk_max_market_data_age_seconds,
    )

    result = await submit_trade_and_record(
        session,
        broker_id,
        proposal,
        account,
        limits,
        broker_adapter,
        emergency_stop_active=settings.emergency_stop_active,
        submitted_by_user_id=authorized.user.id,
    )

    await save_paper_broker(session, broker_id, broker_adapter)
    await session.commit()

    assert result.order_id is not None  # always set by submit_trade_and_record
    return TradeSubmissionResponse(
        order_id=result.order_id,
        status=OrderStatus(result.status.value),
        approved=result.risk_decision.approved,
        block_reason=result.risk_decision.reason,
        detail=result.risk_decision.detail,
        fill_quantity=result.fill.quantity if result.fill else None,
        fill_price=result.fill.fill_price if result.fill else None,
    )
