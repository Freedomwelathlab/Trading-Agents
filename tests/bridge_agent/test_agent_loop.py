"""The agent loop and its configuration, with a stubbed platform."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from bridge_agent.agent import BridgeAgent
from bridge_agent.config import ConfigError, load_config
from bridge_agent.gateway import GatewayError, GatewayStatus
from bridge_agent.platform_client import PlatformClient, PlatformError

TOKEN = "a" * 40


class FakeGateway:
    name = "ibkr"

    def __init__(self, *, fail: bool = False, authenticated: bool = True) -> None:
        self.fail = fail
        self.authenticated = authenticated
        self.handled: list[tuple[str, dict]] = []
        self.tickles = 0

    def status(self) -> GatewayStatus:
        return GatewayStatus(True, self.authenticated, "ok")

    def keepalive(self) -> None:
        self.tickles += 1

    def handle(self, kind: str, payload: dict[str, Any], *, job_id: str) -> dict[str, Any]:
        self.handled.append((kind, payload))
        if self.fail:
            raise GatewayError("IBKR refused")
        return {"cash_by_currency": {"USD": "1"}}

    def close(self) -> None:
        pass


class StubPlatform:
    def __init__(self, jobs: list[dict[str, Any] | None]) -> None:
        self.jobs = jobs
        self.requests: list[tuple[str, dict, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((request.url.path, body, dict(request.headers)))
        if request.url.path == "/bridge/agent/claim":
            return httpx.Response(200, json={"job": self.jobs.pop(0) if self.jobs else None})
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json={"outcome": "recorded"})
        return httpx.Response(200, json={"ok": True, "server_time": "2026-01-01T00:00:00Z"})


def make(jobs, gateway=None, clock=lambda: 100.0):
    stub = StubPlatform(jobs)
    platform = PlatformClient(
        base_url="https://api.example",
        token=TOKEN,
        agent_id="pc",
        claim_wait_seconds=1,
        transport=httpx.MockTransport(stub),
    )
    gw = gateway or FakeGateway()
    agent = BridgeAgent(platform=platform, gateways={"ibkr": gw}, clock=clock)
    return agent, stub, gw


def job(kind: str = "account_read", expires_in: float = 20.0) -> dict[str, Any]:
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "provider": "ibkr",
        "kind": kind,
        "payload": {"account_id": "DU1"},
        "expires_in_seconds": expires_in,
    }


def test_process_one_claims_runs_and_posts_the_result_with_the_token():
    agent, stub, gw = make([job()])
    assert agent.process_one() is True
    assert gw.handled == [("account_read", {"account_id": "DU1"})]
    path, body, headers = stub.requests[-1]
    assert path.endswith("/result")
    assert body == {
        "agent_id": "pc",
        "ok": True,
        "result": {"cash_by_currency": {"USD": "1"}},
        "error": None,
    }
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert stub.requests[0][1]["providers"] == ["ibkr"]


def test_no_job_means_nothing_runs():
    agent, stub, gw = make([None])
    assert agent.process_one() is False
    assert gw.handled == []


def test_a_gateway_refusal_is_reported_as_a_failure():
    agent, stub, _ = make([job()], gateway=FakeGateway(fail=True))
    agent.process_one()
    body = stub.requests[-1][1]
    assert body["ok"] is False and body["error"] == "IBKR refused"


def test_an_order_with_too_little_time_left_is_refused_before_the_gateway():
    agent, stub, gw = make([job("order_submit", expires_in=5.0)])
    agent.process_one()
    assert gw.handled == [], "an order must not start with the platform about to give up"
    assert "EXPIRED_BEFORE_EXECUTION" in stub.requests[-1][1]["error"]


def test_elapsed_time_counts_against_the_deadline():
    times = iter([0.0, 30.0])
    agent, _, gw = make([], clock=lambda: next(times))
    ok, _, error = agent.execute(job(expires_in=20.0), received_at=0.0)
    assert (ok, gw.handled) == (True, [("account_read", {"account_id": "DU1"})])
    ok, _, error = agent.execute(job(expires_in=20.0), received_at=0.0)
    assert ok is False and "EXPIRED_BEFORE_EXECUTION" in (error or "")


def test_a_provider_the_agent_does_not_serve_is_refused():
    agent, _, _ = make([])
    ok, _, error = agent.execute({**job(), "provider": "moomoo"}, received_at=100.0)
    assert ok is False and "does not serve moomoo" in (error or "")


def test_heartbeat_reports_gateways_and_tickles_only_a_logged_in_gateway():
    agent, stub, gw = make([])
    reports = agent.heartbeat_once()
    assert reports == {"ibkr": {"reachable": True, "authenticated": True, "detail": "ok"}}
    assert stub.requests[-1][0] == "/bridge/agent/heartbeat"
    assert gw.tickles == 1

    logged_out = FakeGateway(authenticated=False)
    agent2, _, _ = make([], gateway=logged_out)
    agent2.heartbeat_once()
    assert logged_out.tickles == 0


def test_a_rejected_token_is_a_platform_error():
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Bridge agent token rejected."})

    platform = PlatformClient(
        base_url="https://api.example",
        token=TOKEN,
        agent_id="pc",
        claim_wait_seconds=1,
        transport=httpx.MockTransport(refuse),
    )
    with pytest.raises(PlatformError, match="401"):
        platform.claim(("ibkr",))


# --- config -----------------------------------------------------------------------


def _env(**over: str) -> dict[str, str]:
    base = {"PLATFORM_URL": "https://api.example", "BRIDGE_AGENT_TOKEN": TOKEN}
    base.update(over)
    return base


def test_config_defaults(tmp_path: Path):
    config = load_config(_env(BRIDGE_AGENT_ID="home pc!"), env_file=tmp_path / "none")
    assert config.providers == ("ibkr",)
    assert config.agent_id == "home-pc-"
    assert config.ibkr_gateway_url == "https://localhost:5000/v1/api"
    assert config.ibkr_verify_tls is False, "localhost's self-signed cert is the one exception"
    assert TOKEN not in repr(config)


def test_config_refuses_unsafe_or_missing_values(tmp_path: Path):
    none = tmp_path / "none"
    with pytest.raises(ConfigError, match="https"):
        load_config(_env(PLATFORM_URL="http://api.example"), env_file=none)
    with pytest.raises(ConfigError, match="32"):
        load_config(_env(BRIDGE_AGENT_TOKEN="short"), env_file=none)
    with pytest.raises(ConfigError, match="BRIDGE_PROVIDERS"):
        load_config(_env(BRIDGE_PROVIDERS="kraken"), env_file=none)
    with pytest.raises(ConfigError, match="PLATFORM_URL"):
        load_config({"BRIDGE_AGENT_TOKEN": TOKEN}, env_file=none)
    local = load_config(_env(PLATFORM_URL="http://localhost:8000"), env_file=none)
    assert local.platform_url == "http://localhost:8000"


def test_a_remote_gateway_is_tls_verified(tmp_path: Path):
    config = load_config(
        _env(IBKR_GATEWAY_URL="https://gw.example:5000/v1/api"), env_file=tmp_path / "none"
    )
    assert config.ibkr_verify_tls is True


def test_env_file_is_read_and_the_environment_wins(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# comment\nPLATFORM_URL="https://from-file.example"\n'
        f"BRIDGE_AGENT_TOKEN={TOKEN}\nBRIDGE_PROVIDERS=ibkr,moomoo\n",
        encoding="utf-8",
    )
    config = load_config({"BRIDGE_PROVIDERS": "moomoo"}, env_file=env_file)
    assert config.platform_url == "https://from-file.example"
    assert config.providers == ("moomoo",)
