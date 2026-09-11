"""The `StrategyDeployment` lifecycle state machine (Phase 63, D081).

`create_deployment` touches the DB (it validates the version and the
broker), so these run against real Postgres like the runner tests, but they
assert only on the state transitions and the guardrails - the runner's own
tests cover what happens when a deployment is actually run.
"""

import uuid

import pytest

from apps.api.app.db.models import (
    Broker,
    BrokerKind,
    StrategyDeploymentStatus,
    StrategyVersion,
    StrategyVersionStatus,
)
from apps.api.app.deployments.service import (
    DeploymentError,
    approve_deployment,
    create_deployment,
    pause_deployment,
    resume_deployment,
    stop_deployment,
)
from tests.deployments.conftest import db_session, deployment_world


async def _new(session, world, **overrides):
    kwargs = dict(
        strategy_version_id=world["version_id"],
        broker_id=world["broker_id"],
        symbols=["aapl.us", "AAPL.US ", "msft.us"],
        bar_interval="1d",
        mode="paper",
        requested_by_user_id=None,
    )
    kwargs.update(overrides)
    return await create_deployment(session, **kwargs)


@pytest.mark.asyncio
async def test_create_normalizes_and_dedupes_symbols_and_starts_pending_approval() -> None:
    async with db_session() as session, deployment_world(session) as w:
        dep = await _new(session, w)
        await session.commit()
        assert dep.status is StrategyDeploymentStatus.PENDING_APPROVAL
        assert dep.symbols == ["AAPL.US", "MSFT.US"]  # trimmed, upper, deduped, ordered
        assert dep.approved_by_user_id is None and dep.approved_at is None
        assert dep.mode == "paper"


@pytest.mark.asyncio
async def test_create_rejects_a_mode_that_is_neither_paper_nor_live() -> None:
    async with db_session() as session, deployment_world(session) as w:
        with pytest.raises(DeploymentError, match="UNSUPPORTED_MODE"):
            await _new(session, w, mode="paper_and_a_bit_live")


@pytest.mark.asyncio
async def test_create_accepts_live_mode_against_a_live_broker_still_pending_approval() -> None:
    """Phase 64, D082: a live deployment is real data, not yet real trading -
    it lands PENDING_APPROVAL exactly like a paper one."""
    async with (
        db_session() as session,
        deployment_world(session, with_live_broker=True) as w,
    ):
        dep = await _new(session, w, broker_id=w["live_broker_id"], mode="live")
        await session.commit()
        assert dep.status is StrategyDeploymentStatus.PENDING_APPROVAL
        assert dep.mode == "live"


@pytest.mark.asyncio
async def test_create_rejects_a_live_mode_deployment_against_a_paper_broker() -> None:
    async with db_session() as session, deployment_world(session) as w:
        with pytest.raises(DeploymentError, match="NOT_A_LIVE_BROKER"):
            await _new(session, w, mode="live")  # w["broker_id"] is PAPER


@pytest.mark.asyncio
async def test_create_still_rejects_a_paper_mode_deployment_against_a_live_broker() -> None:
    async with (
        db_session() as session,
        deployment_world(session, with_live_broker=True) as w,
    ):
        with pytest.raises(DeploymentError, match="NOT_A_PAPER_BROKER"):
            await _new(session, w, broker_id=w["live_broker_id"])  # mode defaults to paper


@pytest.mark.asyncio
async def test_create_rejects_an_unvalidated_version() -> None:
    async with db_session() as session, deployment_world(session) as w:
        version = await session.get(StrategyVersion, w["version_id"])
        version.status = StrategyVersionStatus.DRAFT
        await session.flush()
        with pytest.raises(DeploymentError, match="VERSION_NOT_VALIDATED"):
            await _new(session, w)


@pytest.mark.asyncio
async def test_create_rejects_an_unknown_broker_and_a_live_broker() -> None:
    async with db_session() as session, deployment_world(session) as w:
        with pytest.raises(DeploymentError, match="NO_SUCH_BROKER"):
            await _new(session, w, broker_id=uuid.uuid4())

        live_id = uuid.uuid4()
        session.add(
            Broker(id=live_id, name="live", kind=BrokerKind.LIVE, provider="longbridge")
        )
        await session.flush()
        with pytest.raises(DeploymentError, match="NOT_A_PAPER_BROKER"):
            await _new(session, w, broker_id=live_id)


@pytest.mark.asyncio
async def test_create_rejects_an_empty_and_an_over_cap_symbol_list() -> None:
    async with db_session() as session, deployment_world(session) as w:
        with pytest.raises(DeploymentError, match="EMPTY_SYMBOL_LIST"):
            await _new(session, w, symbols=["  ", ""])
        with pytest.raises(DeploymentError, match="TOO_MANY_SYMBOLS"):
            await _new(session, w, symbols=[f"S{i}.US" for i in range(51)])


@pytest.mark.asyncio
async def test_the_full_lifecycle_approve_pause_resume_stop() -> None:
    async with db_session() as session, deployment_world(session) as w:
        dep = await _new(session, w)
        approver = uuid.uuid4()

        await approve_deployment(dep, approved_by_user_id=approver)
        assert dep.status is StrategyDeploymentStatus.ACTIVE
        assert dep.approved_by_user_id == approver and dep.approved_at is not None

        await pause_deployment(dep, reason="cooling off")
        assert dep.status is StrategyDeploymentStatus.PAUSED
        assert dep.paused_reason == "cooling off"

        await resume_deployment(dep)
        assert dep.status is StrategyDeploymentStatus.ACTIVE
        assert dep.paused_reason is None  # cleared on resume

        await stop_deployment(dep)
        assert dep.status is StrategyDeploymentStatus.STOPPED
        assert dep.stopped_at is not None


@pytest.mark.asyncio
async def test_transitions_are_rejected_from_the_wrong_state() -> None:
    async with db_session() as session, deployment_world(session) as w:
        dep = await _new(session, w)

        with pytest.raises(DeploymentError, match="NOT_ACTIVE"):
            await pause_deployment(dep, reason=None)  # still pending_approval
        with pytest.raises(DeploymentError, match="NOT_PAUSED"):
            await resume_deployment(dep)

        await approve_deployment(dep, approved_by_user_id=None)
        with pytest.raises(DeploymentError, match="NOT_PENDING_APPROVAL"):
            await approve_deployment(dep, approved_by_user_id=None)  # already active

        await stop_deployment(dep)
        with pytest.raises(DeploymentError, match="ALREADY_STOPPED"):
            await stop_deployment(dep)
        with pytest.raises(DeploymentError, match="NOT_PENDING_APPROVAL"):
            await approve_deployment(dep, approved_by_user_id=None)  # stopped is terminal
