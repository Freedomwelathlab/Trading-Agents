"""Integration tests for the Phase 63 strategy-deployment routes (D081),
against real Postgres.

The lifecycle state machine and the runner have their own tests
(`tests/deployments/`); this file covers the HTTP surface: the two
permissions (`strategy:deploy` for everything, `strategy:approve_deployment`
for the approval action alone), per-request ownership, the create-time
guardrails surfaced as 409s, and that the transitions are wired to the
right endpoints.
"""

import contextlib
import uuid

import pytest
from sqlalchemy import delete, select

from apps.api.app.auth.permissions import Permission
from apps.api.app.auth.security import hash_password
from apps.api.app.db.models import (
    Broker,
    BrokerKind,
    Role,
    Strategy,
    StrategyDeployment,
    StrategyDeploymentRun,
    StrategyVersion,
    User,
)
from tests.api.test_admin import (
    TEST_PASSWORD,
    _get_token,
    api_client,
    db_session,
    non_admin_user,
)
from tests.api.test_strategies import VALID_DEFINITION
from tests.api.test_strategy_backtests import SMA2_DEFINITION, _validated_strategy


@contextlib.asynccontextmanager
async def _role_user(session, *, permissions: list[str], label: str):
    role_id = uuid.uuid4()
    user_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    session.add(Role(id=role_id, name=f"test-{label}-{role_id}", permissions=permissions))
    session.add(
        User(
            id=user_id,
            email=email,
            hashed_password=hash_password(TEST_PASSWORD),
            is_active=True,
            role_id=role_id,
        )
    )
    await session.commit()
    try:
        yield user_id, email
    finally:
        await session.rollback()
        version_ids = select(StrategyVersion.id).where(
            StrategyVersion.strategy_id.in_(
                select(Strategy.id).where(Strategy.owner_user_id == user_id)
            )
        )
        dep_ids = select(StrategyDeployment.id).where(
            StrategyDeployment.strategy_version_id.in_(version_ids)
        )
        await session.execute(
            delete(StrategyDeploymentRun).where(
                StrategyDeploymentRun.deployment_id.in_(dep_ids)
            )
        )
        await session.execute(
            delete(StrategyDeployment).where(StrategyDeployment.id.in_(dep_ids))
        )
        await session.execute(delete(Strategy).where(Strategy.owner_user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Role).where(Role.id == role_id))
        await session.commit()


@contextlib.asynccontextmanager
async def deploy_user(session):
    """The full-capability owner: manage + deploy + approve."""
    async with _role_user(
        session,
        permissions=[
            Permission.STRATEGY_MANAGE.value,
            Permission.STRATEGY_DEPLOY.value,
            Permission.STRATEGY_APPROVE_DEPLOYMENT.value,
        ],
        label="deploy",
    ) as pair:
        yield pair


@contextlib.asynccontextmanager
async def deploy_only_user(session):
    """Can create/list/pause/stop a deployment but CANNOT approve one."""
    async with _role_user(
        session,
        permissions=[Permission.STRATEGY_MANAGE.value, Permission.STRATEGY_DEPLOY.value],
        label="deployonly",
    ) as pair:
        yield pair


@contextlib.asynccontextmanager
async def paper_broker(session):
    broker_id = uuid.uuid4()
    session.add(
        Broker(id=broker_id, name="pb", kind=BrokerKind.PAPER, provider="paper-sim")
    )
    await session.commit()
    try:
        yield broker_id
    finally:
        await session.rollback()
        dep_ids = select(StrategyDeployment.id).where(
            StrategyDeployment.broker_id == broker_id
        )
        await session.execute(
            delete(StrategyDeploymentRun).where(
                StrategyDeploymentRun.deployment_id.in_(dep_ids)
            )
        )
        await session.execute(
            delete(StrategyDeployment).where(StrategyDeployment.broker_id == broker_id)
        )
        await session.execute(delete(Broker).where(Broker.id == broker_id))
        await session.commit()


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_a_manage_only_caller_cannot_touch_the_deployment_surface() -> None:
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r1 = await client.get(f"/deployments/{uuid.uuid4()}", headers=_h(token))
            r2 = await client.post(
                f"/strategies/{uuid.uuid4()}/versions/{uuid.uuid4()}/deployments",
                headers=_h(token),
                json={"broker_id": str(uuid.uuid4()), "symbols": ["X.US"]},
            )
    assert r1.status_code == 403
    assert r2.status_code == 403


@pytest.mark.asyncio
async def test_create_leaves_it_pending_and_approval_needs_the_separate_permission() -> None:
    async with (
        db_session() as session,
        deploy_only_user(session) as (_uid, email),
        paper_broker(session) as broker_id,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            strategy_id, version_id = await _validated_strategy(
                client, token, definition=SMA2_DEFINITION
            )
            created = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/deployments",
                headers=_h(token),
                json={"broker_id": str(broker_id), "symbols": ["good.us", "GOOD.US "]},
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["status"] == "pending_approval"
            assert body["symbols"] == ["GOOD.US"]  # normalized + deduped
            assert body["mode"] == "paper"
            deployment_id = body["id"]

            # strategy:deploy alone cannot approve.
            denied = await client.post(
                f"/deployments/{deployment_id}/approve", headers=_h(token), json={}
            )
            assert denied.status_code == 403

            # ... and it is still pending.
            got = await client.get(f"/deployments/{deployment_id}", headers=_h(token))
            assert got.json()["status"] == "pending_approval"


@pytest.mark.asyncio
async def test_the_full_http_lifecycle_with_the_approver_permission() -> None:
    async with (
        db_session() as session,
        deploy_user(session) as (_uid, email),
        paper_broker(session) as broker_id,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            strategy_id, version_id = await _validated_strategy(
                client, token, definition=SMA2_DEFINITION
            )
            created = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/deployments",
                headers=_h(token),
                json={"broker_id": str(broker_id), "symbols": ["GOOD.US"]},
            )
            deployment_id = created.json()["id"]

            approved = await client.post(
                f"/deployments/{deployment_id}/approve", headers=_h(token), json={}
            )
            assert approved.status_code == 200
            assert approved.json()["status"] == "active"
            assert approved.json()["approved_at"] is not None

            # Double-approve is a 409.
            assert (
                await client.post(
                    f"/deployments/{deployment_id}/approve", headers=_h(token), json={}
                )
            ).status_code == 409

            paused = await client.post(
                f"/deployments/{deployment_id}/pause",
                headers=_h(token),
                json={"reason": "manual hold"},
            )
            assert paused.json()["status"] == "paused"
            assert paused.json()["paused_reason"] == "manual hold"

            resumed = await client.post(
                f"/deployments/{deployment_id}/resume", headers=_h(token)
            )
            assert resumed.json()["status"] == "active"
            assert resumed.json()["paused_reason"] is None

            stopped = await client.post(
                f"/deployments/{deployment_id}/stop", headers=_h(token)
            )
            assert stopped.json()["status"] == "stopped"

            # Terminal: cannot pause a stopped deployment.
            assert (
                await client.post(
                    f"/deployments/{deployment_id}/pause", headers=_h(token), json={}
                )
            ).status_code == 409

            runs = await client.get(
                f"/deployments/{deployment_id}/runs", headers=_h(token)
            )
            assert runs.status_code == 200
            assert runs.json() == {"items": [], "limit": 50, "offset": 0}

            listing = await client.get(
                f"/strategies/{strategy_id}/versions/{version_id}/deployments",
                headers=_h(token),
            )
            assert [d["id"] for d in listing.json()["items"]] == [deployment_id]


@pytest.mark.asyncio
async def test_create_guardrails_are_409s() -> None:
    async with (
        db_session() as session,
        deploy_user(session) as (_uid, email),
        paper_broker(session) as broker_id,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            strategy_id, version_id = await _validated_strategy(
                client, token, definition=SMA2_DEFINITION
            )
            url = f"/strategies/{strategy_id}/versions/{version_id}/deployments"

            live_mode = await client.post(
                url,
                headers=_h(token),
                json={"broker_id": str(broker_id), "symbols": ["X.US"], "mode": "live"},
            )
            assert live_mode.status_code == 422  # Literal["paper"] rejects it at the schema

            unknown_broker = await client.post(
                url,
                headers=_h(token),
                json={"broker_id": str(uuid.uuid4()), "symbols": ["X.US"]},
            )
            assert unknown_broker.status_code == 409
            assert "NO_SUCH_BROKER" in unknown_broker.text

            empty = await client.post(
                url, headers=_h(token), json={"broker_id": str(broker_id), "symbols": []}
            )
            assert empty.status_code == 422  # min_length=1

            # A draft version cannot be deployed.
            draft = await client.post(
                "/strategies",
                headers=_h(token),
                json={"name": "draft", "definition": VALID_DEFINITION},
            )
            d_body = draft.json()
            draft_resp = await client.post(
                f"/strategies/{d_body['id']}/versions/{d_body['latest_version']['id']}/deployments",
                headers=_h(token),
                json={"broker_id": str(broker_id), "symbols": ["X.US"]},
            )
            assert draft_resp.status_code == 409
            assert "VERSION_NOT_VALIDATED" in draft_resp.text


@pytest.mark.asyncio
async def test_another_users_deployment_is_403_and_an_unknown_id_is_404() -> None:
    async with db_session() as session:
        async with (
            deploy_user(session) as (_owner_uid, owner_email),
            deploy_user(session) as (_other_uid, other_email),
            paper_broker(session) as broker_id,
        ):
            async with api_client() as client:
                owner_token = await _get_token(client, owner_email)
                other_token = await _get_token(client, other_email)
                strategy_id, version_id = await _validated_strategy(
                    client, owner_token, definition=SMA2_DEFINITION
                )
                created = await client.post(
                    f"/strategies/{strategy_id}/versions/{version_id}/deployments",
                    headers=_h(owner_token),
                    json={"broker_id": str(broker_id), "symbols": ["GOOD.US"]},
                )
                deployment_id = created.json()["id"]

                assert (
                    await client.get(
                        f"/deployments/{deployment_id}", headers=_h(other_token)
                    )
                ).status_code == 403
                assert (
                    await client.post(
                        f"/deployments/{deployment_id}/approve",
                        headers=_h(other_token),
                        json={},
                    )
                ).status_code == 403
                assert (
                    await client.get(
                        f"/deployments/{uuid.uuid4()}", headers=_h(owner_token)
                    )
                ).status_code == 404
