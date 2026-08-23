"""Minimal admin surface: create/update users and roles, create/revoke
broker grants, without raw SQL. Every route requires the admin:manage
permission (Permission.ADMIN) - there is no finer-grained admin
permission model this phase (see docs/DECISIONS.md D013/D016).

Bootstrap note: the very first admin user and role still have to be
created by direct DB insert - there is no user holding admin:manage to
call these routes with the first time. Everything after that first
bootstrap can go through this API instead of SQL.

Deliberately still not built: listing endpoints, self-service
registration, deleting a role/user outright (would orphan FKs from
orders.submitted_by_user_id / users.role_id - deactivate via
is_active=false instead). This remains the minimum surface D010/D011/D012
needed, not a general admin panel.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.schemas_admin import (
    BrokerGrantResponse,
    CreateBrokerGrantRequest,
    CreateRoleRequest,
    CreateRoleResponse,
    CreateUserRequest,
    CreateUserResponse,
    UpdateRoleRequest,
    UpdateUserRequest,
)
from apps.api.app.auth.dependencies import require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.auth.security import hash_password
from apps.api.app.db.base import get_session
from apps.api.app.db.models import Broker, BrokerGrant, Role, User

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_permission(Permission.ADMIN))],
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
