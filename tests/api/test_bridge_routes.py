"""The bridge agent's server side (Phase 104, D124), against real Postgres.

What these pin down:

* the agent routes exist only when BRIDGE_AGENT_TOKEN is set (and long
  enough), and accept only that token - never a user's session;
* a job moves queued -> claimed -> done/failed exactly once, and a job the
  platform stopped waiting for can never be claimed afterwards;
* a late answer is still recorded (for the audit trail) and answered 409;
* the connection probe for IBKR runs END TO END through the queue: the probe
  enqueues, a fake agent claims over HTTP and answers, and the probe reports
  the venue's figure - or NOT_CONNECTED, fast, when no agent is alive.

No test here places or previews an order at any venue: the "agent" is this
test file.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from apps.api.app.bridge.channel import (
    BridgeNotConnectedError,
    BridgeTimeoutError,
    DatabaseBridgeChannel,
)
from apps.api.app.bridge.store import (
    JobKind,
    claim_next_job,
    enqueue_job,
    expire_overdue_jobs,
)
from apps.api.app.core.config import get_settings
from apps.api.app.db.base import get_session_factory
from apps.api.app.db.models import BridgeAgentHeartbeat, BridgeJob
from tests.api.test_admin import _get_token, admin_user, api_client, db_session, non_admin_user

TOKEN = "t" * 48
READY = {"ibkr": {"reachable": True, "authenticated": True, "detail": "authenticated"}}


def _agent(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _user(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@contextlib.asynccontextmanager
async def bridge_settings(token: str | None = TOKEN, ttl: int | None = None):
    settings = get_settings()
    previous = (settings.bridge_agent_token, settings.bridge_job_ttl_seconds)
    settings.bridge_agent_token = token
    if ttl is not None:
        settings.bridge_job_ttl_seconds = ttl
    try:
        yield settings
    finally:
        settings.bridge_agent_token, settings.bridge_job_ttl_seconds = previous


@contextlib.asynccontextmanager
async def clean_bridge_tables():
    """Every test starts and ends with no jobs and no heartbeats, so the
    `latest heartbeat` one test writes can never make another pass."""

    async def wipe() -> None:
        async with get_session_factory()() as session:
            await session.execute(delete(BridgeJob))
            await session.execute(delete(BridgeAgentHeartbeat))
            await session.commit()

    await wipe()
    try:
        yield
    finally:
        await wipe()


# --- auth ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_unset_disables_every_agent_route():
    async with bridge_settings(token=None), clean_bridge_tables(), api_client() as client:
        for path, body in (
            ("/bridge/agent/claim", {"agent_id": "a", "providers": ["ibkr"], "wait_seconds": 0}),
            ("/bridge/agent/heartbeat", {"agent_id": "a", "gateways": {}}),
        ):
            r = await client.post(path, json=body, headers=_agent())
            assert r.status_code == 503, r.text
            assert "NOT_CONFIGURED" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_short_token_is_treated_as_unset():
    async with bridge_settings(token="short"), clean_bridge_tables(), api_client() as client:
        r = await client.post(
            "/bridge/agent/heartbeat", json={"agent_id": "a"}, headers=_agent("short")
        )
    assert r.status_code == 503
    assert "shorter than 32" in r.json()["detail"]


@pytest.mark.asyncio
async def test_wrong_token_and_user_session_are_both_refused():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with bridge_settings(), clean_bridge_tables(), api_client() as client:
            user_token = await _get_token(client, email)
            body = {"agent_id": "a", "gateways": {}}
            wrong = await client.post(
                "/bridge/agent/heartbeat", json=body, headers=_agent("x" * 48)
            )
            as_user = await client.post(
                "/bridge/agent/heartbeat", json=body, headers=_user(user_token)
            )
            missing = await client.post("/bridge/agent/heartbeat", json=body)
            right = await client.post("/bridge/agent/heartbeat", json=body, headers=_agent())
    assert wrong.status_code == 401
    assert as_user.status_code == 401, "an admin's session must not act as the agent"
    assert missing.status_code == 401
    assert right.status_code == 200


# --- lifecycle ----------------------------------------------------------------------


async def _enqueue(kind: JobKind = JobKind.ACCOUNT_READ, ttl: int = 30) -> uuid.UUID:
    async with get_session_factory()() as session:
        job = await enqueue_job(
            session, provider="ibkr", kind=kind, payload={"account_id": "DU1"}, ttl_seconds=ttl
        )
        await session.commit()
        return job.id


@pytest.mark.asyncio
async def test_claim_result_lifecycle_and_no_double_claim():
    async with bridge_settings(), clean_bridge_tables(), api_client() as client:
        job_id = await _enqueue()
        claim = {"agent_id": "pc", "providers": ["ibkr"], "wait_seconds": 0}

        first = (await client.post("/bridge/agent/claim", json=claim, headers=_agent())).json()
        assert first["job"]["id"] == str(job_id)
        assert first["job"]["payload"] == {"account_id": "DU1"}
        assert 0 < first["job"]["expires_in_seconds"] <= 30
        second = (await client.post("/bridge/agent/claim", json=claim, headers=_agent())).json()
        assert second["job"] is None, "a claimed job must never be handed out twice"

        other = await client.post(
            f"/bridge/agent/jobs/{job_id}/result",
            json={"agent_id": "someone-else", "ok": True, "result": {"x": 1}},
            headers=_agent(),
        )
        assert other.status_code == 409

        done = await client.post(
            f"/bridge/agent/jobs/{job_id}/result",
            json={"agent_id": "pc", "ok": True, "result": {"cash_by_currency": {"USD": "5"}}},
            headers=_agent(),
        )
        assert done.status_code == 200, done.text
        again = await client.post(
            f"/bridge/agent/jobs/{job_id}/result",
            json={"agent_id": "pc", "ok": False, "error": "late overwrite"},
            headers=_agent(),
        )
        assert again.status_code == 409, "a finished job cannot be rewritten"

        async with get_session_factory()() as session:
            row = (
                await session.execute(select(BridgeJob).where(BridgeJob.id == job_id))
            ).scalar_one()
        assert row.status == "done"
        assert row.result == {"cash_by_currency": {"USD": "5"}}
        assert row.claimed_by == "pc"


@pytest.mark.asyncio
async def test_claim_only_matches_the_providers_asked_for():
    async with bridge_settings(), clean_bridge_tables(), api_client() as client:
        await _enqueue()
        r = await client.post(
            "/bridge/agent/claim",
            json={"agent_id": "pc", "providers": ["moomoo"], "wait_seconds": 0},
            headers=_agent(),
        )
        assert r.json()["job"] is None
        bad = await client.post(
            "/bridge/agent/claim",
            json={"agent_id": "pc", "providers": ["kraken"], "wait_seconds": 0},
            headers=_agent(),
        )
        assert bad.status_code == 422


@pytest.mark.asyncio
async def test_an_expired_job_is_never_claimed_and_a_late_answer_is_recorded():
    async with bridge_settings(), clean_bridge_tables(), api_client() as client:
        stale = await _enqueue(ttl=30)
        async with get_session_factory()() as session:
            row = await session.get(BridgeJob, stale)
            assert row is not None
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        r = await client.post(
            "/bridge/agent/claim",
            json={"agent_id": "pc", "providers": ["ibkr"], "wait_seconds": 0},
            headers=_agent(),
        )
        assert r.json()["job"] is None
        async with get_session_factory()() as session:
            assert (await session.get(BridgeJob, stale)).status == "expired"  # type: ignore[union-attr]

        # Claimed, then the platform gives up: the answer still lands, as LATE.
        late = await _enqueue(ttl=30)
        claimed = await client.post(
            "/bridge/agent/claim",
            json={"agent_id": "pc", "providers": ["ibkr"], "wait_seconds": 0},
            headers=_agent(),
        )
        assert claimed.json()["job"]["id"] == str(late)
        async with get_session_factory()() as session:
            row = await session.get(BridgeJob, late)
            assert row is not None
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
            await expire_overdue_jobs(session)
            await session.commit()
        answer = await client.post(
            f"/bridge/agent/jobs/{late}/result",
            json={"agent_id": "pc", "ok": True, "result": {"order_id": "123"}},
            headers=_agent(),
        )
        assert answer.status_code == 409
        assert "JOB_EXPIRED" in answer.json()["detail"]
        async with get_session_factory()() as session:
            row = await session.get(BridgeJob, late)
        assert row is not None and row.status == "expired"
        assert row.result == {"order_id": "123"}, "a late answer must be kept for the audit"


@pytest.mark.asyncio
async def test_a_payload_with_a_credential_like_key_is_refused_before_it_is_written():
    from apps.api.app.bridge.store import SecretInPayloadError

    async with clean_bridge_tables(), get_session_factory()() as session:
        with pytest.raises(SecretInPayloadError):
            await enqueue_job(
                session,
                provider="ibkr",
                kind=JobKind.ACCOUNT_READ,
                payload={"account_id": "DU1", "nested": {"trade_password": "x"}},
                ttl_seconds=10,
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_claim_skip_locked_never_hands_one_job_to_two_sessions():
    async with bridge_settings(), clean_bridge_tables():
        await _enqueue()
        factory = get_session_factory()
        async with factory() as a, factory() as b:
            got_a = await claim_next_job(a, providers=["ibkr"], agent_id="a")
            got_b = await claim_next_job(b, providers=["ibkr"], agent_id="b")
            assert got_a is not None
            assert got_b is None
            await a.commit()
            await b.commit()


# --- status -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_is_admin_only_and_reports_the_heartbeat():
    async with db_session() as session, admin_user(session) as (_uid, admin_email):
        async with non_admin_user(session) as (_uid2, user_email):
            async with bridge_settings(), clean_bridge_tables(), api_client() as client:
                admin = await _get_token(client, admin_email)
                user = await _get_token(client, user_email)
                assert (await client.get("/bridge/status", headers=_user(user))).status_code == 403

                before = (await client.get("/bridge/status", headers=_user(admin))).json()
                assert before["configured"] is True
                assert before["agent"] is None
                assert before["providers"]["ibkr"].startswith("NOT_CONNECTED")

                await client.post(
                    "/bridge/agent/heartbeat",
                    json={
                        "agent_id": "home-pc",
                        "agent_version": "1.0.0",
                        "gateways": {
                            "ibkr": {
                                "reachable": True,
                                "authenticated": False,
                                "detail": "not logged in",
                            }
                        },
                    },
                    headers=_agent(),
                )
                after = (await client.get("/bridge/status", headers=_user(admin))).json()
    assert after["agent"]["agent_id"] == "home-pc"
    assert after["agent"]["connected"] is True
    assert after["providers"]["ibkr"].startswith("GATEWAY_NOT_AUTHENTICATED")
    assert after["providers"]["moomoo"].startswith("NOT_CONNECTED")


@pytest.mark.asyncio
async def test_status_says_not_configured_without_a_token():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with bridge_settings(token=None), clean_bridge_tables(), api_client() as client:
            admin = await _get_token(client, email)
            body = (await client.get("/bridge/status", headers=_user(admin))).json()
    assert body["configured"] is False
    assert all(v.startswith("NOT_CONFIGURED") for v in body["providers"].values())


# --- the channel and the probe, end to end ------------------------------------------


async def _fake_agent(client, *, answer, rounds: int = 10) -> list[dict]:
    """Claim over HTTP and answer each job with `answer(job)`."""
    seen: list[dict] = []
    for _ in range(rounds):
        r = await client.post(
            "/bridge/agent/claim",
            json={"agent_id": "fake-pc", "providers": ["ibkr"], "wait_seconds": 2},
            headers=_agent(),
        )
        job = r.json()["job"]
        if job is None:
            continue
        seen.append(job)
        ok, result, error = answer(job)
        await client.post(
            f"/bridge/agent/jobs/{job['id']}/result",
            json={"agent_id": "fake-pc", "ok": ok, "result": result, "error": error},
            headers=_agent(),
        )
        if len(seen) >= 2:
            break
    return seen


@pytest.mark.asyncio
async def test_the_ibkr_probe_reads_the_account_through_the_agent(monkeypatch):
    monkeypatch.setenv("IBKR_ACCOUNT_ID", "DU1234567")

    def answer(job):
        assert job["payload"]["account_id"] == "DU1234567"
        if job["kind"] == "account_read":
            return True, {"cash_by_currency": {"USD": "1000.50", "BASE": "1000.50"}}, None
        return True, {"positions": [{"symbol": "265598", "quantity": "10"}]}, None

    async with db_session() as session, admin_user(session) as (_uid, email):
        async with bridge_settings(), clean_bridge_tables(), api_client() as client:
            token = await _get_token(client, email)
            await client.post(
                "/bridge/agent/heartbeat",
                json={"agent_id": "fake-pc", "gateways": READY},
                headers=_agent(),
            )
            probe, jobs = await asyncio.gather(
                client.post("/brokers/providers/ibkr/connection-check", headers=_user(token)),
                _fake_agent(client, answer=answer),
            )

    assert probe.status_code == 200, probe.text
    body = probe.json()
    assert body["reachable"] is True, body
    assert body["cash"] == "1000.50"
    assert body["position_symbols"] == ["265598"]
    assert "through the bridge agent" in body["detail"]
    assert [j["kind"] for j in jobs] == ["account_read", "positions_read"]
    assert {"name": "account_id", "label": "Account id", "value": "DU1234567"} in body["account"]


@pytest.mark.asyncio
async def test_the_probe_says_not_connected_immediately_without_an_agent(monkeypatch):
    monkeypatch.setenv("IBKR_ACCOUNT_ID", "DU1234567")
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with bridge_settings(), clean_bridge_tables(), api_client() as client:
            token = await _get_token(client, email)
            r = await client.post("/brokers/providers/ibkr/connection-check", headers=_user(token))
            async with get_session_factory()() as s:
                queued = (await s.execute(select(BridgeJob))).scalars().all()
    body = r.json()
    assert body["reachable"] is False
    assert "NOT_CONNECTED" in body["detail"]
    assert body["cash"] is None
    assert queued == [], "nothing may be queued for an agent that is not there"


@pytest.mark.asyncio
async def test_the_probe_is_not_configured_without_a_bridge_token(monkeypatch):
    monkeypatch.setenv("IBKR_ACCOUNT_ID", "DU1234567")
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with bridge_settings(token=None), api_client() as client:
            token = await _get_token(client, email)
            r = await client.post("/brokers/providers/ibkr/connection-check", headers=_user(token))
    assert r.json()["reachable"] is False
    assert "NOT_CONFIGURED" in r.json()["detail"]
    assert "BRIDGE_AGENT_TOKEN" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_job_nobody_claims_times_out_and_is_expired_never_answered():
    async with bridge_settings(ttl=1) as settings, clean_bridge_tables():
        async with get_session_factory()() as session:
            from apps.api.app.bridge.store import record_heartbeat

            await record_heartbeat(session, agent_id="pc", agent_version=None, gateways=READY)
            await session.commit()
        channel = DatabaseBridgeChannel(
            loop=asyncio.get_running_loop(),
            session_factory=get_session_factory(),
            settings=settings,
            poll_interval=0.1,
        )
        with pytest.raises(BridgeTimeoutError) as err:
            await asyncio.to_thread(
                channel.call,
                provider="ibkr",
                kind=JobKind.ACCOUNT_READ,
                payload={"account_id": "DU1"},
            )
        assert "nothing was sent" in str(err.value)
        async with get_session_factory()() as session:
            rows = (await session.execute(select(BridgeJob))).scalars().all()
        assert [r.status for r in rows] == ["expired"]


@pytest.mark.asyncio
async def test_a_stale_heartbeat_is_not_connected():
    async with bridge_settings() as settings, clean_bridge_tables():
        async with get_session_factory()() as session:
            from apps.api.app.bridge.store import record_heartbeat

            await record_heartbeat(
                session,
                agent_id="pc",
                agent_version=None,
                gateways=READY,
                now=datetime.now(UTC) - timedelta(minutes=5),
            )
            await session.commit()
        channel = DatabaseBridgeChannel(
            loop=asyncio.get_running_loop(),
            session_factory=get_session_factory(),
            settings=settings,
        )
        with pytest.raises(BridgeNotConnectedError) as err:
            await asyncio.to_thread(
                channel.call, provider="ibkr", kind=JobKind.ACCOUNT_READ, payload={}
            )
    assert "last heard from" in str(err.value)


@pytest.mark.asyncio
async def test_calling_the_channel_on_the_event_loop_is_refused_not_deadlocked():
    from apps.api.app.bridge.channel import BridgeError

    async with bridge_settings() as settings:
        channel = DatabaseBridgeChannel(
            loop=asyncio.get_running_loop(),
            session_factory=get_session_factory(),
            settings=settings,
        )
        with pytest.raises(BridgeError, match="off the event loop"):
            channel.call(provider="ibkr", kind=JobKind.ACCOUNT_READ, payload={})
