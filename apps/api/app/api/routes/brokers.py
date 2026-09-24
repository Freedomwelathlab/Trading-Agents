"""Broker discovery for the authenticated caller (docs/DECISIONS.md D034).

Every other broker-scoped route in this codebase takes a `broker_id` in
its path and verifies it with require_broker_access. Until this module
existed there was no way for a user to learn what those ids are: a
non-admin trader had to be told a UUID out-of-band, and the only listing
that existed (`GET /admin/broker-grants`, D031) is gated on admin:manage
and returns grants for every user, not the caller's own.

Scoping rule: a BrokerGrant row is what makes a broker discoverable.
`GET /brokers` returns exactly the brokers the calling user holds a grant
for - it is the read side of the same D012 grant model that
require_broker_access enforces, so the list can never advertise a broker
whose trade/portfolio routes would then 403. A user with no grants gets an
empty list, not a 403: having zero access is a real, correct answer to
"what may I reach", not an authorization failure.

Authentication only (get_current_user), no permission check: the result is
already scoped to the caller's own grants, so there is nothing here that a
coarser permission would additionally protect. Requiring VIEW_PORTFOLIO or
SUBMIT_PAPER_TRADE would also be wrong in both directions - it would hide
brokers from a user who genuinely holds a grant but only one of those
permissions, and it would not narrow what is returned for anyone else.

Never returns any credential-shaped field - the response is the explicit
allow-list in apps/api/app/api/schemas_brokers.py, not the ORM row.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.schemas_brokers import BrokerResponse, ListBrokersResponse
from apps.api.app.auth.dependencies import get_current_user
from apps.api.app.db.base import get_session
from apps.api.app.db.models import Broker, BrokerGrant, User

router = APIRouter(prefix="/brokers", tags=["brokers"])

DEFAULT_LIST_LIMIT = 50
"""Same default page size as the admin listings (D031) and the portfolio
history endpoint (D027) - deliberately one convention, not a third."""
MAX_LIST_LIMIT = 500
"""Same hard ceiling, same reasoning: a caller wanting more pages rather
than the server ever building an unbounded response."""


@router.get("", response_model=ListBrokersResponse)
async def list_accessible_brokers(
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    approved_only: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListBrokersResponse:
    """The brokers this user holds a BrokerGrant for, ordered by
    `(name, id)`. Name alone is not unique on `brokers`, so the id is the
    tiebreaker that makes the sort total and `offset` paging
    deterministic.

    `approved_only` (Phase 97, D116) narrows to brokers an admin has
    approved (`is_active`). The trading desk asks for it; every other
    caller keeps the unfiltered list BrokerResponse's docstring defends,
    because a bot or a history view still needs to name a broker the desk
    no longer offers."""
    conditions = [BrokerGrant.user_id == current_user.id]
    if approved_only:
        conditions.append(Broker.is_active.is_(True))
    rows = (
        (
            await session.execute(
                select(Broker)
                .join(BrokerGrant, BrokerGrant.broker_id == Broker.id)
                .where(*conditions)
                .order_by(Broker.name.asc(), Broker.id.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListBrokersResponse(
        brokers=[
            BrokerResponse(
                id=row.id,
                name=row.name,
                kind=row.kind,
                provider=row.provider,
                is_active=row.is_active,
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
    )


@router.get("/{broker_id}", response_model=BrokerResponse)
async def get_accessible_broker(
    broker_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BrokerResponse:
    """404 when the broker does not exist, 403 when it exists but the
    caller holds no grant for it - the same order and the same two status
    codes require_broker_access uses, so this route and the trade/portfolio
    routes never disagree about a given broker_id.

    This deliberately re-implements that check rather than depending on
    require_broker_access: that dependency additionally demands a
    Permission, and there is no single permission that means "may look up
    a broker I already have a grant for" (see this module's docstring).
    """
    broker = (
        await session.execute(select(Broker).where(Broker.id == broker_id))
    ).scalar_one_or_none()
    if broker is None:
        raise HTTPException(status_code=404, detail=f"No broker with id {broker_id}.")

    grant = (
        await session.execute(
            select(BrokerGrant).where(
                BrokerGrant.user_id == current_user.id,
                BrokerGrant.broker_id == broker_id,
            )
        )
    ).scalar_one_or_none()
    if grant is None:
        raise HTTPException(status_code=403, detail=f"No access grant for broker {broker_id}.")

    return BrokerResponse(
        id=broker.id,
        name=broker.name,
        kind=broker.kind,
        provider=broker.provider,
        is_active=broker.is_active,
    )
