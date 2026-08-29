"""The emergency stop's persisted source of truth (docs/DECISIONS.md D039).

This module deliberately lives OUTSIDE apps/api/app/risk/. The Risk Engine
is zero-I/O by construction (see apps/api/app/risk/engine.py's docstring)
and `evaluate_trade()` keeps taking `emergency_stop_active` as a plain
boolean parameter. The database read that produces that boolean happens
here, one layer above, and is handed down as data - exactly the same
discipline D024's recent-orders query and D029's Portfolio Manager follow.
Nothing in this file may ever be imported by apps/api/app/risk/.

State model: `emergency_stop_events` is append-only, and the current state
is the `active` value of the highest-`id` row. An empty table means the
switch has never been flipped, in which case `Settings.emergency_stop_active`
is the fallback default. That fallback is a bootstrap value only - the
moment one row exists, `Settings` is never consulted again, which is the
whole point: flipping the stop must not require an `.env` edit and a
redeploy.

Freshness: the state is read from the database on every trade submission
rather than cached in the process, so a flip made through the API takes
effect on the very next trade in every worker, with no restart and no
cache-invalidation channel. This is one indexed single-row read on a path
that already performs several queries; the safety property is worth more
than the microseconds.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import EmergencyStopEvent

MAX_REASON_LENGTH = 500
"""Matches EmergencyStopEvent.reason's column width. Enforced on the
request schema too, so an over-long reason is a 422, never a truncated
audit record."""


@dataclass(frozen=True)
class EmergencyStopState:
    """The current state plus the provenance of how it got that way.

    `source` is "database" once any row exists and "settings_default" while
    the table is still empty - callers (and GET /admin/emergency-stop)
    report it verbatim so it is always visible whether the persisted row or
    the `.env` fallback is in force. Never guessed: with no row, the other
    fields are None rather than a fabricated actor/timestamp.
    """

    active: bool
    source: str
    reason: str | None = None
    actor_user_id: uuid.UUID | None = None
    changed_at: datetime | None = None


async def get_emergency_stop_state(
    session: AsyncSession, *, settings_default: bool
) -> EmergencyStopState:
    """Read the authoritative current state. `settings_default` is used if
    and only if no row has ever been written."""
    latest = (
        await session.execute(
            select(EmergencyStopEvent).order_by(EmergencyStopEvent.id.desc()).limit(1)
        )
    ).scalar_one_or_none()

    if latest is None:
        return EmergencyStopState(active=settings_default, source="settings_default")

    return EmergencyStopState(
        active=latest.active,
        source="database",
        reason=latest.reason,
        actor_user_id=latest.actor_user_id,
        changed_at=latest.created_at,
    )


async def is_emergency_stop_active(session: AsyncSession, *, settings_default: bool) -> bool:
    """The single boolean the trade path hands to the (still pure) Risk
    Engine."""
    state = await get_emergency_stop_state(session, settings_default=settings_default)
    return state.active


async def set_emergency_stop(
    session: AsyncSession, *, active: bool, reason: str, actor_user_id: uuid.UUID | None
) -> EmergencyStopState:
    """Append one flip. Does NOT commit - the caller owns the transaction,
    matching how every other write in this codebase is composed.

    A no-op flip (activating an already-active stop) still writes a row:
    the table is an audit log of attempts to change the control, and
    silently dropping one would lose the record that somebody tried.
    """
    event = EmergencyStopEvent(
        active=active, reason=reason, actor_user_id=actor_user_id
    )
    session.add(event)
    await session.flush()
    # created_at is a server_default; an explicit refresh loads the real
    # DB-assigned timestamp instead of leaving an expired attribute whose
    # later access would be a lazy load in async context.
    await session.refresh(event)
    return EmergencyStopState(
        active=event.active,
        source="database",
        reason=event.reason,
        actor_user_id=event.actor_user_id,
        changed_at=event.created_at,
    )
