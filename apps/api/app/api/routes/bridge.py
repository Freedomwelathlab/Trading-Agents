"""The bridge agent's HTTP surface (Phase 104, D124).

Two routers:

* `/bridge/agent/*` - called by the agent on the operator's PC, never by a
  person. Authenticated by `BRIDGE_AGENT_TOKEN`, a bearer token compared as
  a SHA-256 digest with `hmac.compare_digest`. It is NOT a user session and
  grants nothing else in the API: the agent can claim jobs, post results
  and heartbeat, and that is all. Unset token -> every route here answers
  503 NOT_CONFIGURED, so a deployment that never set one has no bridge
  surface at all.
* `GET /bridge/status` - for an admin: is an agent connected, which
  gateways it reaches, what is queued.

The traffic is PULL-ONLY from the PC's side. The agent opens every
connection; the PC listens on nothing, so nothing on the internet can
reach its gateways through this design.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from apps.api.app.auth.dependencies import require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.bridge.store import (
    BRIDGED_PROVIDERS,
    CompleteOutcome,
    claim_next_job,
    complete_job,
    expire_overdue_jobs,
    gateway_readiness,
    latest_heartbeat,
    queue_counts,
    recent_jobs,
    record_heartbeat,
)
from apps.api.app.core.config import BRIDGE_AGENT_TOKEN_MIN_LENGTH, Settings, get_settings
from apps.api.app.db.base import get_session_factory
from apps.api.app.db.models import User

agent_router = APIRouter(prefix="/bridge/agent", tags=["bridge"])
status_router = APIRouter(prefix="/bridge", tags=["bridge"])

AGENT_ID_PATTERN = r"^[A-Za-z0-9_.\-]{1,64}$"
MAX_RESULT_BYTES = 256 * 1024
"""A result larger than this is refused: nothing the agent legitimately
returns (a balance, a position list, an order status) comes near it."""

CLAIM_POLL_SECONDS = 0.5

BridgedProvider = Literal["ibkr", "moomoo"]


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def bridge_not_configured_reason(settings: Settings) -> str | None:
    if not settings.bridge_agent_token:
        return "NOT_CONFIGURED: BRIDGE_AGENT_TOKEN is not set, so the bridge agent is disabled."
    if not settings.bridge_enabled:
        return (
            f"NOT_CONFIGURED: BRIDGE_AGENT_TOKEN is shorter than "
            f"{BRIDGE_AGENT_TOKEN_MIN_LENGTH} characters and is ignored."
        )
    return None


async def require_bridge_agent(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """The agent's only credential. Deliberately not `get_current_user`:
    the agent is not a user, holds no role and must not be able to reach
    any user route with this token."""
    reason = bridge_not_configured_reason(settings)
    if reason is not None:
        raise HTTPException(status_code=503, detail=reason)
    scheme, _, presented = (authorization or "").partition(" ")
    expected = settings.bridge_agent_token or ""
    if (
        scheme.lower() != "bearer"
        or not presented
        or not hmac.compare_digest(_digest(presented.strip()), _digest(expected))
    ):
        raise HTTPException(
            status_code=401,
            detail="Bridge agent token rejected.",
            headers={"WWW-Authenticate": "Bearer"},
        )


# --- schemas -------------------------------------------------------------------


class ClaimRequest(BaseModel):
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    providers: list[BridgedProvider] = Field(min_length=1)
    wait_seconds: float = Field(default=20.0, ge=0, le=60)


class ClaimedJob(BaseModel):
    id: uuid.UUID
    provider: str
    kind: str
    payload: dict[str, Any]
    created_at: datetime
    expires_in_seconds: float
    """Relative, not absolute, so the agent's own clock never has to agree
    with the server's. The agent refuses to START an order job with too
    little of this left."""


class ClaimResponse(BaseModel):
    job: ClaimedJob | None = None


class ResultRequest(BaseModel):
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=4000)


class ResultResponse(BaseModel):
    job_id: uuid.UUID
    outcome: str


class GatewayReport(BaseModel):
    reachable: bool
    authenticated: bool
    detail: str = Field(default="", max_length=500)


class HeartbeatRequest(BaseModel):
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    agent_version: str | None = Field(default=None, max_length=32)
    gateways: dict[BridgedProvider, GatewayReport] = Field(default_factory=dict)


class HeartbeatResponse(BaseModel):
    ok: bool
    server_time: datetime


# --- agent routes -----------------------------------------------------------------


@agent_router.post(
    "/claim", response_model=ClaimResponse, dependencies=[Depends(require_bridge_agent)]
)
async def claim(payload: ClaimRequest, settings: Settings = Depends(get_settings)) -> ClaimResponse:
    """Long-poll: hand out the oldest claimable job for these providers, or
    wait up to `wait_seconds` (capped by BRIDGE_CLAIM_MAX_WAIT_SECONDS) and
    answer `{"job": null}`. No database connection is held while waiting."""
    factory = get_session_factory()
    wait = min(payload.wait_seconds, float(settings.bridge_claim_max_wait_seconds))
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        async with factory() as session:
            now = datetime.now(UTC)
            await expire_overdue_jobs(session, now=now)
            job = await claim_next_job(
                session, providers=payload.providers, agent_id=payload.agent_id, now=now
            )
            if job is not None:
                claimed = ClaimedJob(
                    id=job.id,
                    provider=job.provider,
                    kind=job.kind,
                    payload=dict(job.payload or {}),
                    created_at=job.created_at,
                    expires_in_seconds=max(0.0, (job.expires_at - now).total_seconds()),
                )
            await session.commit()
        if job is not None:
            return ClaimResponse(job=claimed)
        if asyncio.get_running_loop().time() >= deadline:
            return ClaimResponse(job=None)
        await asyncio.sleep(CLAIM_POLL_SECONDS)


@agent_router.post(
    "/jobs/{job_id}/result",
    response_model=ResultResponse,
    dependencies=[Depends(require_bridge_agent)],
)
async def post_result(job_id: uuid.UUID, payload: ResultRequest) -> ResultResponse:
    """Record the gateway's answer. A late answer (the platform already
    stopped waiting) is STILL written - for an order it is exactly the row
    an operator needs - and answered 409 so the agent logs it loudly."""
    if payload.result is not None and len(json.dumps(payload.result)) > MAX_RESULT_BYTES:
        raise HTTPException(status_code=413, detail="Result too large.")
    if payload.ok and payload.result is None:
        raise HTTPException(status_code=422, detail="An ok result needs a `result` object.")
    async with get_session_factory()() as session:
        outcome = await complete_job(
            session,
            job_id,
            agent_id=payload.agent_id,
            ok=payload.ok,
            result=payload.result,
            error=payload.error,
        )
        await session.commit()
    if outcome is CompleteOutcome.NOT_FOUND:
        raise HTTPException(status_code=404, detail=f"No bridge job {job_id}.")
    if outcome is CompleteOutcome.LATE:
        raise HTTPException(
            status_code=409,
            detail=(
                "JOB_EXPIRED: the platform had already stopped waiting for this job. The "
                "result was recorded for the audit trail and nothing acted on it."
            ),
        )
    if outcome is not CompleteOutcome.RECORDED:
        raise HTTPException(status_code=409, detail=f"{outcome.value.upper()}: not recorded.")
    return ResultResponse(job_id=job_id, outcome=outcome.value)


@agent_router.post(
    "/heartbeat", response_model=HeartbeatResponse, dependencies=[Depends(require_bridge_agent)]
)
async def heartbeat(payload: HeartbeatRequest) -> HeartbeatResponse:
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        await record_heartbeat(
            session,
            agent_id=payload.agent_id,
            agent_version=payload.agent_version,
            gateways={name: report.model_dump() for name, report in payload.gateways.items()},
            now=now,
        )
        await session.commit()
    return HeartbeatResponse(ok=True, server_time=now)


# --- admin status ----------------------------------------------------------------


class AgentView(BaseModel):
    agent_id: str
    agent_version: str | None
    last_seen_at: datetime
    age_seconds: float
    connected: bool
    gateways: dict[str, Any]


class RecentJobView(BaseModel):
    id: uuid.UUID
    provider: str
    kind: str
    status: str
    created_at: datetime
    claimed_at: datetime | None
    completed_at: datetime | None
    error: str | None


class BridgeStatusResponse(BaseModel):
    configured: bool
    detail: str
    stale_after_seconds: int
    agent: AgentView | None
    providers: dict[str, str]
    """Per bridged venue: `ready`, or the exact reason a job would be
    refused right now (NOT_CONNECTED / GATEWAY_UNREACHABLE /
    GATEWAY_NOT_AUTHENTICATED)."""
    queue: dict[str, int]
    recent_jobs: list[RecentJobView]


@status_router.get("/status", response_model=BridgeStatusResponse)
async def bridge_status(
    _current_user: User = Depends(require_permission(Permission.ADMIN)),
    settings: Settings = Depends(get_settings),
) -> BridgeStatusResponse:
    """Presence and freshness only. Payloads are not returned (they hold
    no secret, but an account's order history is still not a status)."""
    reason = bridge_not_configured_reason(settings)
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        beat = await latest_heartbeat(session)
        counts = await queue_counts(session)
        jobs = await recent_jobs(session)
    stale = settings.bridge_agent_stale_seconds
    agent = None
    if beat is not None:
        age = (now - beat.last_seen_at).total_seconds()
        agent = AgentView(
            agent_id=beat.agent_id,
            agent_version=beat.agent_version,
            last_seen_at=beat.last_seen_at,
            age_seconds=round(age, 1),
            connected=age <= stale,
            gateways=dict(beat.gateways or {}),
        )
    providers = {
        name: (
            reason
            if reason is not None
            else gateway_readiness(beat, name, now=now, stale_seconds=stale).detail
        )
        for name in sorted(BRIDGED_PROVIDERS)
    }
    return BridgeStatusResponse(
        configured=reason is None,
        detail=reason or "Bridge enabled.",
        stale_after_seconds=stale,
        agent=agent,
        providers=providers,
        queue=counts,
        recent_jobs=[
            RecentJobView(
                id=j.id,
                provider=j.provider,
                kind=j.kind,
                status=j.status,
                created_at=j.created_at,
                claimed_at=j.claimed_at,
                completed_at=j.completed_at,
                error=j.error,
            )
            for j in jobs
        ],
    )
