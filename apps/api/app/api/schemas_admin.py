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


class CreateRoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = None
    permissions: list[str] = Field(default_factory=list)


class CreateRoleResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    permissions: list[str]


class CreateBrokerGrantRequest(BaseModel):
    user_id: uuid.UUID
    broker_id: uuid.UUID


class BrokerGrantResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    broker_id: uuid.UUID
