"""The emergency stop's HTTP surface (docs/DECISIONS.md D039).

Two routers, deliberately, both under /admin:

- `router` (writes: activate/deactivate) carries the same router-level
  `admin:manage` dependency every other admin write route carries. Flipping
  the platform-wide kill switch is at least as privileged as creating a
  user, so it gets exactly the same gate - no new finer-grained permission
  (same reasoning as D013's one-coarse-admin-permission choice).

- `status_router` (GET) requires authentication only. Knowing whether the
  system is halted is not privileged information - a trader whose orders
  are all being rejected is entitled to see WHY without holding
  admin:manage, and denying them that just produces a confused user
  guessing at a broker outage. This is the same scoping argument D034 made
  for broker discovery: seeing that you're blocked is not the same
  capability as being able to block others. Flipping the switch stays
  admin-only.

The GET response includes the reason and actor of the flip in force. That
is intentional and is the point of an operational halt notice: everyone
affected should be able to read "halted at 14:02 by <admin> because
<reason>". It exposes no credential and no other user's data beyond the
acting admin's id.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.schemas_admin import EmergencyStopRequest, EmergencyStopStatusResponse
from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import User
from apps.api.app.safety.emergency_stop import (
    EmergencyStopState,
    get_emergency_stop_state,
    set_emergency_stop,
)

router = APIRouter(
    prefix="/admin",
    tags=["admin", "safety"],
    dependencies=[Depends(require_permission(Permission.ADMIN))],
)
status_router = APIRouter(prefix="/admin", tags=["admin", "safety"])
logger = get_logger(__name__)


def _to_response(state: EmergencyStopState) -> EmergencyStopStatusResponse:
    return EmergencyStopStatusResponse(
        active=state.active,
        source=state.source,
        reason=state.reason,
        actor_user_id=state.actor_user_id,
        changed_at=state.changed_at,
    )


@status_router.get("/emergency-stop", response_model=EmergencyStopStatusResponse)
async def get_emergency_stop_status(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
) -> EmergencyStopStatusResponse:
    """Any authenticated active user. Reads the same persisted state the
    trade path reads, from the database - never a process-local cache, so
    what this returns is what the next trade will actually be evaluated
    against."""
    state = await get_emergency_stop_state(
        session, settings_default=settings.emergency_stop_active
    )
    return _to_response(state)


@router.post("/emergency-stop", response_model=EmergencyStopStatusResponse)
async def activate_emergency_stop(
    request: EmergencyStopRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
) -> EmergencyStopStatusResponse:
    """Halt all trading. Takes effect on the very next trade submission in
    every worker process - no restart, no `.env` edit, no redeploy (D039).
    The `reason` is required and is persisted with the acting user's id."""
    state = await set_emergency_stop(
        session, active=True, reason=request.reason, actor_user_id=current_user.id
    )
    await session.commit()
    logger.warning(
        "emergency_stop_activated",
        actor_user_id=str(current_user.id),
        reason=request.reason,
    )
    return _to_response(state)


@router.post("/emergency-stop/deactivate", response_model=EmergencyStopStatusResponse)
async def deactivate_emergency_stop(
    request: EmergencyStopRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
) -> EmergencyStopStatusResponse:
    """Resume trading. A separate endpoint rather than a boolean field on
    one route, so "turn the safety control OFF" can never be the accidental
    result of a malformed or defaulted body - it has to be an explicitly
    different URL, and it too requires a recorded reason."""
    state = await set_emergency_stop(
        session, active=False, reason=request.reason, actor_user_id=current_user.id
    )
    await session.commit()
    logger.warning(
        "emergency_stop_deactivated",
        actor_user_id=str(current_user.id),
        reason=request.reason,
    )
    return _to_response(state)
