"""How a synchronous broker adapter asks the bridge agent something and
waits for the answer (Phase 104, D124).

`BrokerAdapter` is a SYNCHRONOUS Protocol - every caller runs it off the
event loop (`asyncio.to_thread`, as the connection probe does) - while the
job queue lives behind the async engine. `DatabaseBridgeChannel` bridges
the two by scheduling its coroutine back onto the API's own loop with
`run_coroutine_threadsafe`, so the queue shares the one connection pool
every request uses instead of opening a second one per call.

The answers are exact about what did and did not happen, because for an
ORDER the difference is the whole point:

* `BridgeNotConnectedError` - checked BEFORE anything is queued. Nothing
  was sent anywhere.
* `BridgeTimeoutError` - queued, never claimed, now expired. The agent
  never saw it; nothing was sent to the venue.
* `BridgeOutcomeUnknownError` - the agent CLAIMED it and did not answer in
  time. For a read that is just a failure; for an order it MAY have reached
  the venue, and the message says to check there before retrying.
* `BridgeJobFailedError` - the agent answered "no", carrying the gateway's
  own words.

None of them is ever turned into a result. A timeout is not an empty
account and not a fill.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app.bridge.store import (
    JobKind,
    JobStatus,
    enqueue_job,
    expire_job_if_pending,
    gateway_readiness,
    get_job,
    latest_heartbeat,
)
from apps.api.app.core.config import Settings


class BridgeError(Exception):
    """Base for every bridge failure. Never a result."""


class BridgeNotConfiguredError(BridgeError):
    """BRIDGE_AGENT_TOKEN is unset (or too short): the bridge is off."""


class BridgeNotConnectedError(BridgeError):
    """No fresh heartbeat, or the gateway it reports is down / logged out."""


class BridgeTimeoutError(BridgeError):
    """Queued and never claimed. Nothing reached the venue."""


class BridgeOutcomeUnknownError(BridgeError):
    """Claimed by the agent and not answered in time. For an order, it may
    have reached the venue."""


class BridgeJobFailedError(BridgeError):
    """The agent ran the job and the gateway said no."""


class BridgeChannel(Protocol):
    def call(
        self,
        *,
        provider: str,
        kind: JobKind,
        payload: Mapping[str, Any],
        broker_id: uuid.UUID | None = None,
    ) -> dict[str, Any]: ...


class DatabaseBridgeChannel:
    """The production channel: the `bridge_jobs` table, polled."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop | None,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        poll_interval: float = 0.25,
    ) -> None:
        self._loop = loop
        self._session_factory = session_factory
        self._settings = settings
        self._poll_interval = poll_interval

    def call(
        self,
        *,
        provider: str,
        kind: JobKind,
        payload: Mapping[str, Any],
        broker_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        if self._loop is None or self._loop.is_closed():
            raise BridgeError(
                "The bridge channel was built outside the API's event loop and cannot reach "
                "the job queue. Build bridged adapters from async code."
            )
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            # Blocking the loop on a coroutine that needs the loop to run is
            # a deadlock, not a slow call.
            raise BridgeError(
                "A bridged adapter is synchronous and must be called off the event loop "
                "(asyncio.to_thread), like every other broker adapter."
            )
        future = asyncio.run_coroutine_threadsafe(
            self.acall(provider=provider, kind=kind, payload=payload, broker_id=broker_id),
            self._loop,
        )
        return future.result(timeout=self._settings.bridge_job_ttl_seconds + 15)

    async def acall(
        self,
        *,
        provider: str,
        kind: JobKind,
        payload: Mapping[str, Any],
        broker_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        settings = self._settings
        async with self._session_factory() as session:
            readiness = gateway_readiness(
                await latest_heartbeat(session),
                provider,
                now=datetime.now(UTC),
                stale_seconds=settings.bridge_agent_stale_seconds,
            )
            if not readiness.ready:
                raise BridgeNotConnectedError(readiness.detail)
            job = await enqueue_job(
                session,
                provider=provider,
                kind=kind,
                payload=payload,
                ttl_seconds=settings.bridge_job_ttl_seconds,
                broker_id=broker_id,
            )
            job_id, deadline = job.id, job.expires_at
            await session.commit()

        while datetime.now(UTC) < deadline:
            await asyncio.sleep(self._poll_interval)
            async with self._session_factory() as session:
                row = await get_job(session, job_id)
            if row is None:  # pragma: no cover - rows are never deleted by this code
                raise BridgeError(f"Bridge job {job_id} disappeared.")
            answered = _answer(row.status, row.result, row.error, job_id=job_id, kind=kind)
            if answered is not None:
                return answered

        async with self._session_factory() as session:
            previous, row = await expire_job_if_pending(session, job_id)
            await session.commit()
        answered = _answer(previous, row.result, row.error, job_id=job_id, kind=kind)
        if answered is not None:
            return answered
        ttl = settings.bridge_job_ttl_seconds
        # `claimed_by`, not `previous`: the overdue sweep may have expired
        # the row first, and what matters is whether an agent ever held it.
        if row.claimed_by is None:
            raise BridgeTimeoutError(
                f"BRIDGE_TIMEOUT: the bridge agent did not pick up this {kind.value} job "
                f"within {ttl} s. It has been expired and will never be executed; nothing was "
                f"sent to {provider}."
            )
        if kind in (JobKind.ORDER_SUBMIT, JobKind.CANCEL):
            raise BridgeOutcomeUnknownError(
                f"BRIDGE_OUTCOME_UNKNOWN: the bridge agent claimed this {kind.value} job "
                f"(id {job_id}) but did not report back within {ttl} s. It MAY have reached "
                f"{provider}. Check the venue's own order list before retrying; any late "
                f"answer is recorded on the job."
            )
        raise BridgeOutcomeUnknownError(
            f"BRIDGE_TIMEOUT: the bridge agent claimed this {kind.value} job but did not "
            f"answer within {ttl} s."
        )


def _answer(
    status: str,
    result: dict[str, Any] | None,
    error: str | None,
    *,
    job_id: uuid.UUID,
    kind: JobKind,
) -> dict[str, Any] | None:
    """A finished job's result, a raised failure, or None while pending."""
    if status == JobStatus.DONE.value:
        if not isinstance(result, dict):
            raise BridgeJobFailedError(
                f"The bridge agent reported {kind.value} job {job_id} done with no result."
            )
        return result
    if status == JobStatus.FAILED.value:
        raise BridgeJobFailedError(error or f"The bridge agent reported {kind.value} failed.")
    if status == JobStatus.EXPIRED.value:
        # Another process gave up on it first (a restart, or the overdue
        # sweep). The caller's timeout branch reports what that means.
        return None
    return None
