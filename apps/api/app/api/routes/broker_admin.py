"""Broker housekeeping for admins: see every broker row, approve which ones
the trading desk shows, and remove the ones nobody wants (Phase 97, D116).

Why this exists. Every broker ever created - by hand, by a test run, by a
bot set-up - stayed on the trading desk forever, because the desk listed
every broker the user held a grant for and nothing could remove one. The
operator asked for two things: tick boxes to delete unwanted brokers, and
a desk that shows only the brokers an admin has approved.

**Approval is `is_active`.** The column has existed since migration 0001
and the create form has always carried it, but nothing read it. The desk
now asks for `approved_only`, so approving a broker is setting that flag
and hiding one is clearing it - no new column, no migration.

**Delete never destroys the audit trail.** `orders` and `fills` are
append-only and keyed by broker (D058's reasoning for refusing a kind flip
applies with more force to a delete). So a broker that has ever placed an
order is ARCHIVED instead - hidden from the desk, history intact - and the
answer says so, per broker, rather than failing the whole batch. A broker
with a bot or strategy deployment that is not STOPPED is refused outright:
removing the broker under a running bot would leave it trading an account
nobody can see.

Only a broker with no orders and nothing running is deleted for real,
together with the rows that exist only because of it: its paper book,
its portfolio snapshots, its credentials and its grants.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.auth.dependencies import require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    AutotradeBot,
    AutotradeBotStatus,
    Broker,
    BrokerAccount,
    BrokerCredential,
    BrokerGrant,
    BrokerKind,
    BrokerPosition,
    Order,
    PortfolioSnapshotRow,
    StrategyDeployment,
    StrategyDeploymentStatus,
    User,
)
from apps.api.app.execution.registry import PROVIDERS

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/brokers", tags=["admin"])


class AdminBrokerRow(BaseModel):
    id: uuid.UUID
    name: str
    kind: BrokerKind
    provider: str
    approved: bool
    """`is_active`, under the name the operator uses for it."""
    adapter_status: str
    """`built_in`, `implemented`, `catalogued`, or `unknown` for a provider
    string the registry has never heard of."""
    created_at: datetime
    order_count: int
    running_bots: int
    running_deployments: int


class AdminBrokerListResponse(BaseModel):
    brokers: list[AdminBrokerRow]


class BrokerIdsRequest(BaseModel):
    broker_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


class ApprovalRequest(BrokerIdsRequest):
    approved: bool


class ApprovalResponse(BaseModel):
    updated: int
    not_found: list[uuid.UUID]


class DeleteOutcome(BaseModel):
    broker_id: uuid.UUID
    name: str | None
    outcome: Literal["deleted", "archived", "refused", "not_found"]
    detail: str


class DeleteResponse(BaseModel):
    results: list[DeleteOutcome]


_NOT_STOPPED_BOT = AutotradeBot.status != AutotradeBotStatus.STOPPED
_NOT_STOPPED_DEPLOYMENT = StrategyDeployment.status != StrategyDeploymentStatus.STOPPED


async def _count(
    session: AsyncSession, column: Any, *where: Any
) -> dict[uuid.UUID, int]:
    statement = select(column, func.count()).where(*where).group_by(column)
    rows = (await session.execute(statement)).all()
    return {broker_id: int(n) for broker_id, n in rows}


@router.get("", response_model=AdminBrokerListResponse)
async def list_all_brokers(
    _admin: User = Depends(require_permission(Permission.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> AdminBrokerListResponse:
    """Every broker row, whoever holds grants on it, with what would stop
    it being deleted. Newest first, because the unwanted ones are usually
    the recent test rows."""
    brokers = (
        (await session.execute(select(Broker).order_by(Broker.created_at.desc(), Broker.id)))
        .scalars()
        .all()
    )
    orders = await _count(session, Order.broker_id)
    bots = await _count(session, AutotradeBot.broker_id, _NOT_STOPPED_BOT)
    deployments = await _count(session, StrategyDeployment.broker_id, _NOT_STOPPED_DEPLOYMENT)
    return AdminBrokerListResponse(
        brokers=[
            AdminBrokerRow(
                id=b.id,
                name=b.name,
                kind=b.kind,
                provider=b.provider,
                approved=b.is_active,
                adapter_status=(
                    PROVIDERS[b.provider].adapter_status if b.provider in PROVIDERS else "unknown"
                ),
                created_at=b.created_at,
                order_count=orders.get(b.id, 0),
                running_bots=bots.get(b.id, 0),
                running_deployments=deployments.get(b.id, 0),
            )
            for b in brokers
        ]
    )


@router.post("/approval", response_model=ApprovalResponse)
async def set_approval(
    request: ApprovalRequest,
    admin: User = Depends(require_permission(Permission.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> ApprovalResponse:
    """Approve (show on the desk) or hide the selected brokers. Hiding
    changes nothing about what a broker can do - its grants, bots and
    history are untouched - only whether the desk lists it."""
    wanted = set(request.broker_ids)
    brokers = (
        (await session.execute(select(Broker).where(Broker.id.in_(wanted)))).scalars().all()
    )
    for b in brokers:
        b.is_active = request.approved
    await session.commit()
    found = {b.id for b in brokers}
    logger.info(
        "brokers_approval_changed",
        approved=request.approved,
        broker_ids=[str(i) for i in sorted(found)],
        actor_user_id=str(admin.id),
    )
    return ApprovalResponse(updated=len(found), not_found=sorted(wanted - found))


@router.post("/delete", response_model=DeleteResponse)
async def delete_brokers(
    request: BrokerIdsRequest,
    admin: User = Depends(require_permission(Permission.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> DeleteResponse:
    """Delete, archive or refuse each selected broker - see the module
    docstring for which, and why. Answered per broker so one broker with
    history does not block removing twenty without."""
    results: list[DeleteOutcome] = []
    for broker_id in dict.fromkeys(request.broker_ids):  # de-duplicated, order kept
        broker = (
            await session.execute(select(Broker).where(Broker.id == broker_id))
        ).scalar_one_or_none()
        if broker is None:
            results.append(
                DeleteOutcome(
                    broker_id=broker_id, name=None, outcome="not_found", detail="No such broker."
                )
            )
            continue

        running_bots = (
            await session.execute(
                select(func.count()).where(AutotradeBot.broker_id == broker_id, _NOT_STOPPED_BOT)
            )
        ).scalar_one()
        running_deployments = (
            await session.execute(
                select(func.count()).where(
                    StrategyDeployment.broker_id == broker_id, _NOT_STOPPED_DEPLOYMENT
                )
            )
        ).scalar_one()
        if running_bots or running_deployments:
            results.append(
                DeleteOutcome(
                    broker_id=broker_id,
                    name=broker.name,
                    outcome="refused",
                    detail=(
                        f"{running_bots} bot(s) and {running_deployments} strategy deployment(s) "
                        f"on this broker are not stopped. Stop them first; removing the broker "
                        f"under them would leave them trading an account nobody can see."
                    ),
                )
            )
            continue

        orders = (
            await session.execute(select(func.count()).where(Order.broker_id == broker_id))
        ).scalar_one()
        any_bot = (
            await session.execute(select(func.count()).where(AutotradeBot.broker_id == broker_id))
        ).scalar_one()
        any_deployment = (
            await session.execute(
                select(func.count()).where(StrategyDeployment.broker_id == broker_id)
            )
        ).scalar_one()
        if orders or any_bot or any_deployment:
            broker.is_active = False
            results.append(
                DeleteOutcome(
                    broker_id=broker_id,
                    name=broker.name,
                    outcome="archived",
                    detail=(
                        f"Kept and hidden from the desk: it has {orders} recorded order(s) and "
                        f"{any_bot + any_deployment} stopped bot/deployment record(s), which are "
                        f"the audit trail and are never deleted."
                    ),
                )
            )
            continue

        for model in (
            BrokerPosition,
            BrokerAccount,
            PortfolioSnapshotRow,
            BrokerCredential,
            BrokerGrant,
        ):
            await session.execute(delete(model).where(model.broker_id == broker_id))
        await session.execute(delete(Broker).where(Broker.id == broker_id))
        results.append(
            DeleteOutcome(
                broker_id=broker_id,
                name=broker.name,
                outcome="deleted",
                detail="Deleted, with its paper book, snapshots, credentials and grants.",
            )
        )

    await session.commit()
    logger.warning(
        "brokers_deleted",
        outcomes={str(r.broker_id): r.outcome for r in results},
        actor_user_id=str(admin.id),
    )
    return DeleteResponse(results=results)
