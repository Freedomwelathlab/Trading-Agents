"""Minimal admin surface: create/update users and roles, create/revoke
broker grants, without raw SQL. Every route requires the admin:manage
permission (Permission.ADMIN) - there is no finer-grained admin
permission model this phase (see docs/DECISIONS.md D013/D016).

Bootstrap note: the very first admin user and role still have to be
created by direct DB insert - there is no user holding admin:manage to
call these routes with the first time. Everything after that first
bootstrap can go through this API instead of SQL.

Listing endpoints (GET /admin/users, /admin/roles, /admin/broker-grants)
were added in D030, closing D013's deliberate no-listing scope cut - they
are read-only, paginated (limit/offset, same convention as D027's
portfolio history), and gated by the same router-level admin:manage
dependency every write route here already carries.

Deliberately still not built: self-service registration, deleting a
role/user outright (would orphan FKs from orders.submitted_by_user_id /
users.role_id - deactivate via is_active=false instead), and any
filter/search parameters on the listings (ordering is a fixed, stable
sort; a caller pages rather than queries). This remains a minimal admin
surface, not a general admin panel.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.schemas_admin import (
    BrokerGrantResponse,
    CreateBrokerGrantRequest,
    CreateBrokerRequest,
    CreateRoleRequest,
    CreateRoleResponse,
    CreateUserRequest,
    CreateUserResponse,
    ListBrokerGrantsResponse,
    ListRolesResponse,
    ListUsersResponse,
    UpdateBrokerModeRequest,
    UpdateRoleRequest,
    UpdateUserRequest,
)
from apps.api.app.api.schemas_brokers import BrokerResponse
from apps.api.app.auth.dependencies import require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.auth.security import hash_password
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    Broker,
    BrokerAccount,
    BrokerGrant,
    BrokerKind,
    BrokerPosition,
    Order,
    Role,
    User,
)

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_permission(Permission.ADMIN))],
)

DEFAULT_LIST_LIMIT = 50
"""Default page size for the D030 listing routes - deliberately identical
to the portfolio history endpoint's DEFAULT_HISTORY_LIMIT (D027) so this
codebase has one pagination convention, not two."""
logger = get_logger(__name__)

_LIVE_KIND_CONFIRMATION_REQUIRED = (
    "LIVE_KIND_CONFIRMATION_REQUIRED: designating a broker as kind='live' makes it "
    'eligible for the real-money execution path. Resend with "confirm_live": true to '
    "state that intent explicitly (docs/DECISIONS.md D058). This alone still does not "
    "enable live trading: an order additionally requires TRADING_MODE=live, "
    "LIVE_TRADING_ENABLED=true, the LONGPORT_LIVE_* credentials, the trade:submit:live "
    'permission, and "confirm": true on that individual trade request.'
)

MAX_LIST_LIMIT = 500
"""Hard ceiling on `limit` regardless of what the caller asks for, same
value and reasoning as the history endpoint's MAX_HISTORY_LIMIT: a caller
needing more pages rather than the server ever building an unbounded
response."""


@router.get("/users", response_model=ListUsersResponse)
async def list_users(
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> ListUsersResponse:
    """Users ordered by `email` ascending - a stable, human-meaningful sort
    that makes `offset` paging deterministic (`users` has no created_at
    column to order by). Never returns a password or password hash: each
    row is the same CreateUserResponse shape the create/update routes
    return (docs/DECISIONS.md D030)."""
    rows = (
        (await session.execute(select(User).order_by(User.email.asc()).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    return ListUsersResponse(
        users=[
            CreateUserResponse(
                id=row.id, email=row.email, is_active=row.is_active, role_id=row.role_id
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
    )


@router.get("/roles", response_model=ListRolesResponse)
async def list_roles(
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> ListRolesResponse:
    """Roles ordered by `name` ascending (unique, so the sort is total and
    `offset` paging is deterministic). `permissions` is returned in full -
    this listing is the only way an admin can see what a role currently
    grants without a direct DB query."""
    rows = (
        (await session.execute(select(Role).order_by(Role.name.asc()).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    return ListRolesResponse(
        roles=[
            CreateRoleResponse(
                id=row.id,
                name=row.name,
                description=row.description,
                permissions=row.permissions,
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
    )


@router.get("/broker-grants", response_model=ListBrokerGrantsResponse)
async def list_broker_grants(
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> ListBrokerGrantsResponse:
    """Grants ordered by `(user_id, broker_id)` - the pair is unique (a
    user can hold at most one grant per broker, enforced by the 409 on
    create), so the sort is total and `offset` paging is deterministic.
    This is the read side of the D012 grant model: it says who may reach
    which broker, and is the only way to see an existing grant's `id`
    (needed to revoke it) without a direct DB query."""
    rows = (
        (
            await session.execute(
                select(BrokerGrant)
                .order_by(BrokerGrant.user_id.asc(), BrokerGrant.broker_id.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListBrokerGrantsResponse(
        grants=[
            BrokerGrantResponse(id=row.id, user_id=row.user_id, broker_id=row.broker_id)
            for row in rows
        ],
        limit=limit,
        offset=offset,
    )


@router.post("/users", response_model=CreateUserResponse, status_code=201)
async def create_user(
    request: CreateUserRequest,
    session: AsyncSession = Depends(get_session),
) -> CreateUserResponse:
    if request.role_id is not None:
        role = (
            await session.execute(select(Role).where(Role.id == request.role_id))
        ).scalar_one_or_none()
        if role is None:
            raise HTTPException(status_code=404, detail=f"No role with id {request.role_id}.")

    user = User(
        email=request.email,
        hashed_password=hash_password(request.password),
        is_active=request.is_active,
        role_id=request.role_id,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"A user with email {request.email} already exists."
        ) from None

    return CreateUserResponse(
        id=user.id, email=user.email, is_active=user.is_active, role_id=user.role_id
    )


@router.patch("/users/{user_id}", response_model=CreateUserResponse)
async def update_user(
    user_id: uuid.UUID,
    request: UpdateUserRequest,
    session: AsyncSession = Depends(get_session),
) -> CreateUserResponse:
    """`is_active: false` is how a user is deactivated - there is no
    separate delete/deactivate endpoint (deleting the row would orphan
    orders.submitted_by_user_id). A previously-issued JWT stops working
    immediately: get_current_user re-checks is_active on every request,
    it never trusts a cached claim from the token itself."""
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail=f"No user with id {user_id}.")

    fields_set = request.model_fields_set
    if "is_active" in fields_set and request.is_active is None:
        raise HTTPException(status_code=422, detail="is_active cannot be null.")
    if "role_id" in fields_set and request.role_id is not None:
        role = (
            await session.execute(select(Role).where(Role.id == request.role_id))
        ).scalar_one_or_none()
        if role is None:
            raise HTTPException(status_code=404, detail=f"No role with id {request.role_id}.")

    if "is_active" in fields_set:
        assert request.is_active is not None  # guarded above; narrows for mypy
        user.is_active = request.is_active
    if "role_id" in fields_set:
        user.role_id = request.role_id

    await session.commit()
    return CreateUserResponse(
        id=user.id, email=user.email, is_active=user.is_active, role_id=user.role_id
    )


@router.post("/roles", response_model=CreateRoleResponse, status_code=201)
async def create_role(
    request: CreateRoleRequest,
    session: AsyncSession = Depends(get_session),
) -> CreateRoleResponse:
    role = Role(name=request.name, description=request.description, permissions=request.permissions)
    session.add(role)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"A role named {request.name!r} already exists."
        ) from None

    return CreateRoleResponse(
        id=role.id, name=role.name, description=role.description, permissions=role.permissions
    )


@router.patch("/roles/{role_id}", response_model=CreateRoleResponse)
async def update_role(
    role_id: uuid.UUID,
    request: UpdateRoleRequest,
    session: AsyncSession = Depends(get_session),
) -> CreateRoleResponse:
    """`name` is intentionally not updatable here - see UpdateRoleRequest's
    docstring. Replacing `permissions` takes effect on every holder of
    this role immediately, on their very next request (get_current_user
    re-loads the role fresh each time; nothing caches a permission set)."""
    role = (await session.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=404, detail=f"No role with id {role_id}.")

    fields_set = request.model_fields_set
    if "description" in fields_set:
        role.description = request.description
    if "permissions" in fields_set:
        role.permissions = request.permissions if request.permissions is not None else []

    await session.commit()
    return CreateRoleResponse(
        id=role.id, name=role.name, description=role.description, permissions=role.permissions
    )


@router.post("/broker-grants", response_model=BrokerGrantResponse, status_code=201)
async def create_broker_grant(
    request: CreateBrokerGrantRequest,
    session: AsyncSession = Depends(get_session),
) -> BrokerGrantResponse:
    user = (
        await session.execute(select(User).where(User.id == request.user_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail=f"No user with id {request.user_id}.")

    broker = (
        await session.execute(select(Broker).where(Broker.id == request.broker_id))
    ).scalar_one_or_none()
    if broker is None:
        raise HTTPException(status_code=404, detail=f"No broker with id {request.broker_id}.")

    grant = BrokerGrant(user_id=request.user_id, broker_id=request.broker_id)
    session.add(grant)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"User {request.user_id} already has a grant for broker {request.broker_id}.",
        ) from None

    return BrokerGrantResponse(id=grant.id, user_id=grant.user_id, broker_id=grant.broker_id)


@router.post("/brokers", response_model=BrokerResponse, status_code=201)
async def create_broker(
    request: CreateBrokerRequest,
    current_user: User = Depends(require_permission(Permission.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> BrokerResponse:
    """Phase 43 (D058). Creates a broker row with an explicit `kind`, which
    is the per-broker paper/live trading-mode switch the trade endpoint
    routes on."""
    if request.kind is BrokerKind.LIVE and not request.confirm_live:
        raise HTTPException(status_code=400, detail=_LIVE_KIND_CONFIRMATION_REQUIRED)

    broker = Broker(
        name=request.name,
        kind=request.kind,
        provider=request.provider,
        is_active=request.is_active,
    )
    session.add(broker)
    await session.commit()

    logger.info(
        "broker_created",
        broker_id=str(broker.id),
        kind=broker.kind.value,
        actor_user_id=str(current_user.id),
    )
    return BrokerResponse(
        id=broker.id,
        name=broker.name,
        kind=broker.kind,
        provider=broker.provider,
        is_active=broker.is_active,
    )


@router.patch("/brokers/{broker_id}/mode", response_model=BrokerResponse)
async def set_broker_mode(
    broker_id: uuid.UUID,
    request: UpdateBrokerModeRequest,
    current_user: User = Depends(require_permission(Permission.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> BrokerResponse:
    """Phase 43 (D058): flip ONE broker between paper and live execution.

    This is the per-broker trading-mode toggle. It is deliberately its own
    endpoint rather than a field on a general "update broker" route,
    because it is the only broker attribute that changes which adapter
    real orders go to, and it carries preconditions no other field does.

    Refused when the broker already has ANY recorded order. `orders`/`fills`
    are append-only and keyed by `broker_id` alone - they record no
    per-order kind - so flipping a traded broker would retroactively make
    its simulated and real history indistinguishable. That is an
    audit-integrity failure, not an inconvenience: the remedy is a NEW
    broker row, which costs nothing and keeps each history honest.

    Also refused when a paper book exists (`broker_accounts` /
    `broker_positions` rows), so a simulated 100,000 balance can never
    become the identity of a real account.

    A no-op flip (kind already equals the requested one) succeeds without
    those checks - it changes nothing, so there is nothing to protect.
    """
    broker = (
        await session.execute(select(Broker).where(Broker.id == broker_id))
    ).scalar_one_or_none()
    if broker is None:
        raise HTTPException(status_code=404, detail=f"No broker with id {broker_id}.")

    if broker.kind is request.kind:
        return BrokerResponse(
            id=broker.id,
            name=broker.name,
            kind=broker.kind,
            provider=broker.provider,
            is_active=broker.is_active,
        )

    if request.kind is BrokerKind.LIVE and not request.confirm_live:
        raise HTTPException(status_code=400, detail=_LIVE_KIND_CONFIRMATION_REQUIRED)

    order_exists = (
        await session.execute(select(Order.id).where(Order.broker_id == broker_id).limit(1))
    ).scalar_one_or_none()
    if order_exists is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Broker {broker_id} has recorded orders and its kind can no longer be "
                "changed. The orders/fills history is append-only and does not record a "
                "per-order kind, so flipping it would make simulated and real trades "
                "indistinguishable in the audit trail. Create a new broker instead."
            ),
        )

    account_exists = (
        await session.execute(
            select(BrokerAccount.broker_id).where(BrokerAccount.broker_id == broker_id).limit(1)
        )
    ).scalar_one_or_none()
    position_exists = (
        await session.execute(
            select(BrokerPosition.id).where(BrokerPosition.broker_id == broker_id).limit(1)
        )
    ).scalar_one_or_none()
    if account_exists is not None or position_exists is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Broker {broker_id} has a simulated cash/position book and its kind can "
                "no longer be changed. A simulated balance must never become the identity "
                "of a real account. Create a new broker instead."
            ),
        )

    previous = broker.kind
    broker.kind = request.kind
    await session.commit()

    logger.warning(
        "broker_mode_changed",
        broker_id=str(broker_id),
        previous_kind=previous.value,
        new_kind=broker.kind.value,
        actor_user_id=str(current_user.id),
    )
    return BrokerResponse(
        id=broker.id,
        name=broker.name,
        kind=broker.kind,
        provider=broker.provider,
        is_active=broker.is_active,
    )


@router.delete("/broker-grants/{grant_id}", status_code=204)
async def revoke_broker_grant(
    grant_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    grant = (
        await session.execute(select(BrokerGrant).where(BrokerGrant.id == grant_id))
    ).scalar_one_or_none()
    if grant is None:
        raise HTTPException(status_code=404, detail=f"No broker grant with id {grant_id}.")

    await session.delete(grant)
    await session.commit()
