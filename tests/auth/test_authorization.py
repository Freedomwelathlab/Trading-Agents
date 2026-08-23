"""Unit tests for require_permission()'s decision logic in isolation from
HTTP/DB - constructs plain (unpersisted) User/Role objects rather than
going through a request, since the logic itself doesn't touch the database.
"""

import pytest
from fastapi import HTTPException

from apps.api.app.auth.dependencies import require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.db.models import Role, User


def make_user(*, permissions: list[str] | None) -> User:
    user = User(email="test@example.com", hashed_password="irrelevant")
    if permissions is not None:
        user.role = Role(name="test-role", permissions=permissions)
    return user


@pytest.mark.asyncio
async def test_user_with_the_permission_passes_through():
    checker = require_permission(Permission.SUBMIT_PAPER_TRADE)
    user = make_user(permissions=[Permission.SUBMIT_PAPER_TRADE.value])
    result = await checker(current_user=user)
    assert result is user


@pytest.mark.asyncio
async def test_user_with_no_role_is_rejected():
    checker = require_permission(Permission.SUBMIT_PAPER_TRADE)
    user = make_user(permissions=None)
    with pytest.raises(HTTPException) as exc_info:
        await checker(current_user=user)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_user_with_a_role_lacking_the_permission_is_rejected():
    checker = require_permission(Permission.SUBMIT_PAPER_TRADE)
    user = make_user(permissions=["some:other:permission"])
    with pytest.raises(HTTPException) as exc_info:
        await checker(current_user=user)
    assert exc_info.value.status_code == 403
    assert "trade:submit:paper" in exc_info.value.detail
