"""The bridge job queue and agent heartbeats, in Postgres (Phase 104, D124).

Postgres rather than process memory because the API may run more than one
worker: the agent's claim can land on a different process from the one
waiting for the answer, and only a shared table makes that invisible.

Every function takes an `AsyncSession` and leaves committing to the
caller, the same convention as `execution/credentials.py`.

**Status only moves forward.** queued -> claimed -> done | failed, or
queued/claimed -> expired when the platform stops waiting. Nothing moves a
row backwards, and a job the platform has given up on can never be claimed
again, so a request nobody is waiting for can never reach a venue later.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import BridgeAgentHeartbeat, BridgeJob

BRIDGED_PROVIDERS: frozenset[str] = frozenset({"ibkr", "moomoo"})
"""The venues reached through the agent. A claim for anything else is
refused rather than silently matching nothing."""


class JobKind(str, enum.Enum):  # noqa: UP042 (str mixin matches this codebase)
    ACCOUNT_READ = "account_read"
    POSITIONS_READ = "positions_read"
    ORDER_VALIDATE = "order_validate"
    ORDER_SUBMIT = "order_submit"
    ORDER_STATUS = "order_status"
    CANCEL = "cancel"


class JobStatus(str, enum.Enum):  # noqa: UP042
    QUEUED = "queued"
    CLAIMED = "claimed"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"


PENDING = (JobStatus.QUEUED.value, JobStatus.CLAIMED.value)

_SECRET_KEY_MARKERS = ("password", "secret", "token", "api_key", "apikey", "private")


class SecretInPayloadError(ValueError):
    """A job payload carried a key that looks like a credential.

    Defence in depth, not the primary control: nothing in this codebase
    builds such a payload. It exists so that a future caller who does is
    stopped at the one place every job passes through, before the row is
    written - the whole security model of D124 is that no secret leaves
    the PC and none is sent to it."""


def assert_no_secret_keys(payload: Any, *, path: str = "payload") -> None:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
                raise SecretInPayloadError(
                    f"Refusing to queue a bridge job with a credential-like key "
                    f"{path}.{key}: secrets never travel to the bridge agent (D124)."
                )
            assert_no_secret_keys(value, path=f"{path}.{key}")
    elif isinstance(payload, list | tuple):
        for index, item in enumerate(payload):
            assert_no_secret_keys(item, path=f"{path}[{index}]")


def utcnow() -> datetime:
    return datetime.now(UTC)


async def enqueue_job(
    session: AsyncSession,
    *,
    provider: str,
    kind: JobKind,
    payload: Mapping[str, Any],
    ttl_seconds: int,
    broker_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> BridgeJob:
    if provider not in BRIDGED_PROVIDERS:
        raise ValueError(f"{provider!r} is not reached through the bridge agent.")
    assert_no_secret_keys(payload)
    created = now or utcnow()
    job = BridgeJob(
        id=uuid.uuid4(),
        provider=provider,
        broker_id=broker_id,
        kind=kind.value,
        payload=dict(payload),
        status=JobStatus.QUEUED.value,
        created_at=created,
        expires_at=created + timedelta(seconds=ttl_seconds),
    )
    session.add(job)
    await session.flush()
    return job


async def expire_overdue_jobs(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Mark every pending job past its `expires_at` as expired. Returns the
    count. Called on every claim, so an overdue job is expired before it
    could be handed out even if the process that was waiting for it died."""
    result = await session.execute(
        update(BridgeJob)
        .where(BridgeJob.status.in_(PENDING), BridgeJob.expires_at <= (now or utcnow()))
        .values(status=JobStatus.EXPIRED.value)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def claim_next_job(
    session: AsyncSession,
    *,
    providers: Iterable[str],
    agent_id: str,
    now: datetime | None = None,
) -> BridgeJob | None:
    """The oldest claimable job for these providers, marked claimed.

    `FOR UPDATE SKIP LOCKED` so two agents (or two workers serving one
    agent's overlapping polls) can never both claim one job - a job
    claimed twice is an order placed twice."""
    at = now or utcnow()
    wanted = sorted(set(providers) & BRIDGED_PROVIDERS)
    if not wanted:
        return None
    job = (
        await session.execute(
            select(BridgeJob)
            .where(
                BridgeJob.status == JobStatus.QUEUED.value,
                BridgeJob.provider.in_(wanted),
                BridgeJob.expires_at > at,
            )
            .order_by(BridgeJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if job is None:
        return None
    job.status = JobStatus.CLAIMED.value
    job.claimed_by = agent_id
    job.claimed_at = at
    await session.flush()
    return job


class CompleteOutcome(str, enum.Enum):  # noqa: UP042
    RECORDED = "recorded"
    LATE = "late"
    """The platform had already stopped waiting. The answer is still
    written - for an order it is the row an operator must find - but
    nobody acted on it."""
    NOT_FOUND = "not_found"
    NOT_CLAIMED = "not_claimed"
    WRONG_AGENT = "wrong_agent"
    ALREADY_COMPLETED = "already_completed"


async def complete_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    agent_id: str,
    ok: bool,
    result: Mapping[str, Any] | None,
    error: str | None,
    now: datetime | None = None,
) -> CompleteOutcome:
    job = (
        await session.execute(select(BridgeJob).where(BridgeJob.id == job_id).with_for_update())
    ).scalar_one_or_none()
    if job is None:
        return CompleteOutcome.NOT_FOUND
    if job.status in (JobStatus.DONE.value, JobStatus.FAILED.value):
        return CompleteOutcome.ALREADY_COMPLETED
    if job.status == JobStatus.QUEUED.value:
        return CompleteOutcome.NOT_CLAIMED
    if job.claimed_by is None:
        # Expired while still queued: nobody ever handed it to an agent.
        return CompleteOutcome.NOT_CLAIMED
    if job.claimed_by != agent_id:
        return CompleteOutcome.WRONG_AGENT
    if job.completed_at is not None:
        return CompleteOutcome.ALREADY_COMPLETED

    job.result = dict(result) if result is not None else None
    job.error = None if ok else (error or "the agent reported a failure with no detail")
    job.completed_at = now or utcnow()
    if job.status == JobStatus.EXPIRED.value:
        await session.flush()
        return CompleteOutcome.LATE
    job.status = JobStatus.DONE.value if ok else JobStatus.FAILED.value
    await session.flush()
    return CompleteOutcome.RECORDED


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> BridgeJob | None:
    return (
        await session.execute(select(BridgeJob).where(BridgeJob.id == job_id))
    ).scalar_one_or_none()


async def expire_job_if_pending(session: AsyncSession, job_id: uuid.UUID) -> tuple[str, BridgeJob]:
    """Stop waiting for one job. Returns the status the job had AT THAT
    MOMENT and the row.

    Locked, so a result racing in is either already recorded (and returned
    as done/failed, which the caller then uses) or arrives after and is
    recorded as LATE - never lost, never both."""
    job = (
        await session.execute(select(BridgeJob).where(BridgeJob.id == job_id).with_for_update())
    ).scalar_one()
    previous = job.status
    if previous in PENDING:
        job.status = JobStatus.EXPIRED.value
        await session.flush()
    return previous, job


# --- heartbeats ---------------------------------------------------------------


async def record_heartbeat(
    session: AsyncSession,
    *,
    agent_id: str,
    agent_version: str | None,
    gateways: Mapping[str, Any],
    now: datetime | None = None,
) -> None:
    at = now or utcnow()
    stmt = insert(BridgeAgentHeartbeat).values(
        agent_id=agent_id, last_seen_at=at, agent_version=agent_version, gateways=dict(gateways)
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[BridgeAgentHeartbeat.agent_id],
            set_={
                "last_seen_at": stmt.excluded.last_seen_at,
                "agent_version": stmt.excluded.agent_version,
                "gateways": stmt.excluded.gateways,
            },
        )
    )


async def latest_heartbeat(session: AsyncSession) -> BridgeAgentHeartbeat | None:
    return (
        await session.execute(
            select(BridgeAgentHeartbeat).order_by(BridgeAgentHeartbeat.last_seen_at.desc()).limit(1)
        )
    ).scalar_one_or_none()


async def queue_counts(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(BridgeJob.status, func.count())
            .where(BridgeJob.status.in_(PENDING))
            .group_by(BridgeJob.status)
        )
    ).all()
    counts = {status: 0 for status in PENDING}
    counts.update({str(status): int(n) for status, n in rows})
    return counts


async def recent_jobs(session: AsyncSession, *, limit: int = 20) -> list[BridgeJob]:
    return list(
        (
            await session.execute(
                select(BridgeJob).order_by(BridgeJob.created_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )


@dataclass(frozen=True)
class Readiness:
    ready: bool
    detail: str


def gateway_readiness(
    heartbeat: BridgeAgentHeartbeat | None,
    provider: str,
    *,
    now: datetime,
    stale_seconds: int,
) -> Readiness:
    """Whether a job for `provider` has any chance of being answered.

    Checked BEFORE a job is queued, so "the PC is off" and "the IBKR
    session expired overnight" come back immediately and in those words,
    instead of as a timeout that could mean anything."""
    if heartbeat is None:
        return Readiness(
            False,
            "NOT_CONNECTED: no bridge agent has ever sent a heartbeat. Start the agent on "
            "the PC that runs the gateway (bridge_agent/README.md).",
        )
    age = (now - heartbeat.last_seen_at).total_seconds()
    if age > stale_seconds:
        return Readiness(
            False,
            f"NOT_CONNECTED: the bridge agent {heartbeat.agent_id!r} was last heard from "
            f"{int(age)} s ago (limit {stale_seconds} s). Is the PC on and the agent running?",
        )
    gateway = (heartbeat.gateways or {}).get(provider)
    if not isinstance(gateway, Mapping):
        return Readiness(
            False,
            f"NOT_CONNECTED: the bridge agent is running but does not serve {provider} - "
            f"configure it on the PC.",
        )
    detail = str(gateway.get("detail") or "")
    if not gateway.get("reachable"):
        return Readiness(
            False,
            f"GATEWAY_UNREACHABLE: the agent cannot reach the local {provider} gateway. "
            f"{detail}".strip(),
        )
    if not gateway.get("authenticated"):
        return Readiness(
            False,
            f"GATEWAY_NOT_AUTHENTICATED: the local {provider} gateway is running but not "
            f"logged in. {detail}".strip(),
        )
    return Readiness(True, "ready")
