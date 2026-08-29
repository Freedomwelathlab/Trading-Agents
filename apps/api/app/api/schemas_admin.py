"""DTOs for the /admin routes. Never echo a password or password hash back
in any response - CreateUserResponse deliberately has no such field."""

import uuid

from pydantic import BaseModel, Field


class CreateUserRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8)
    is_active: bool = True
    role_id: uuid.UUID | None = None


class CreateUserResponse(BaseModel):
    id: uuid.UUID
    email: str
    is_active: bool
    role_id: uuid.UUID | None


class UpdateUserRequest(BaseModel):
    """All fields optional - only keys actually present in the JSON body
    are applied (checked via `model_fields_set`, not `is not None`), so
    `{"role_id": null}` unassigns the role while omitting `role_id`
    entirely leaves it untouched. This is how `is_active: false` doubles
    as "deactivate" - no separate delete/deactivate endpoint exists."""

    is_active: bool | None = None
    role_id: uuid.UUID | None = None


class CreateRoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = None
    permissions: list[str] = Field(default_factory=list)


class CreateRoleResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    permissions: list[str]


class UpdateRoleRequest(BaseModel):
    """Same `model_fields_set` convention as `UpdateUserRequest`. `name`
    is deliberately not updatable here - roles are looked up and referenced
    by name in a few places (see tests), and renaming isn't part of this
    endpoint's scope."""

    description: str | None = None
    permissions: list[str] | None = None


class CreateBrokerGrantRequest(BaseModel):
    user_id: uuid.UUID
    broker_id: uuid.UUID


class BrokerGrantResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    broker_id: uuid.UUID


class ListUsersResponse(BaseModel):
    """Paginated listing envelope, same `{items, limit, offset}` shape the
    portfolio history endpoint uses (D027) - see docs/DECISIONS.md D030.
    Reuses CreateUserResponse per row rather than defining a second user
    DTO, so a listed user can never accidentally expose a field the
    create/update responses deliberately omit (password, hash)."""

    users: list[CreateUserResponse]
    limit: int
    offset: int


class ListRolesResponse(BaseModel):
    roles: list[CreateRoleResponse]
    limit: int
    offset: int


class ListBrokerGrantsResponse(BaseModel):
    grants: list[BrokerGrantResponse]
    limit: int
    offset: int
