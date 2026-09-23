"""Liveness and readiness probes (Phase 41, docs/DECISIONS.md D054).

The readiness happy path here is a genuine end-to-end check: it runs the
real route, through the real engine, against the real Postgres the rest of
this suite already requires. It is deliberately NOT mocked — a mocked
"database is reachable" test would assert nothing about the exact thing the
probe exists to establish.

Only the two failure branches use substitutes, because neither can be
produced against a healthy database: the connection-failure case swaps in a
real engine aimed at a port nothing listens on (still a real asyncpg
connection attempt, just a doomed one), and the timeout case swaps in a
stub engine that hangs, since making real Postgres hang on `SELECT 1` is
not something a test may do to a shared database.
"""

import asyncio
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from apps.api.app.api.routes import health as health_module
from apps.api.app.core.config import get_settings
from apps.api.app.main import app


def test_health_reports_research_mode_by_default():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["trading_mode"] == "research"
    assert body["live_trading_enabled"] is False


def test_liveness_does_not_touch_the_database(monkeypatch):
    """The whole point of the split: `/health` must answer even when the
    database is gone, so an orchestrator never restarts a process over a
    dependency outage. Any call to get_engine() here is a regression."""

    def _explode() -> Any:
        raise AssertionError("/health must not open a database connection")

    monkeypatch.setattr(health_module, "get_engine", _explode)

    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readiness_returns_200_against_real_postgres():
    """Happy path — real route, real HTTP layer, real engine, real database.

    Driven through `httpx.ASGITransport` rather than `TestClient` on
    purpose. `TestClient` spins up its own short-lived event loop per
    request, while every other database test in this suite drives the same
    process-wide engine from pytest-asyncio's session loop — and an asyncpg
    connection belongs to the loop that created it, so a pooled connection
    left by an earlier test is unusable from a `TestClient` loop and the
    probe would (correctly, but uninformatively) report a connection
    failure. `ASGITransport` runs the app inside this test's own session
    loop, which is the same loop the pool's connections were made on, i.e.
    the single-loop condition the real uvicorn process always satisfies.
    Nothing is stubbed: the route, the engine, the pool, and the `SELECT 1`
    are all the production ones.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == {"status": "ok"}


def test_readiness_returns_503_when_the_database_is_unreachable(monkeypatch):
    """A real connection attempt to a port nothing listens on."""
    dead_engine = create_async_engine(
        "postgresql+asyncpg://trading_os:trading_os@127.0.0.1:1/trading_os"
    )
    monkeypatch.setattr(health_module, "get_engine", lambda: dead_engine)

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    database = body["checks"]["database"]
    assert database["status"] == "error"
    assert database["reason"] == "connection_failed"
    # The type name is reported; the exception's message (which can carry
    # the DSN, and therefore the password) must never be.
    assert "error_type" in database
    assert "trading_os" not in str(body)


class _HangingEngine:
    """Stands in for an engine whose connection never comes back."""

    def connect(self) -> Any:
        return self

    async def __aenter__(self) -> Any:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def __aexit__(self, *_exc: object) -> None:
        return None


def test_readiness_times_out_rather_than_hanging(monkeypatch):
    """A wedged database must produce a fast, explicit 503 — not a request
    that hangs until the orchestrator's own probe timeout fires."""
    monkeypatch.setattr(health_module, "get_engine", lambda: _HangingEngine())
    monkeypatch.setattr(get_settings(), "health_readiness_timeout_seconds", 0.05)

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["reason"] == "timeout"
    assert body["checks"]["database"]["timeout_seconds"] == pytest.approx(0.05)


def test_health_names_which_llm_variable_is_missing():
    """`NOT_CONFIGURED` alone is not actionable.

    A deployment reported it after the operator had set credentials, and
    nothing on this endpoint could say which half was absent. The variable
    NAMES are the operator's own choice of configuration, not secrets —
    and no value, prefix or length is ever reported.
    """
    from apps.api.app.api.routes.health import _llm_provider_status
    from apps.api.app.core.config import Settings

    def s(**over) -> Settings:
        base = dict(llm_provider_api_key=None, llm_provider_model=None)
        base.update(over)
        return Settings(**base)  # type: ignore[arg-type]

    assert _llm_provider_status(s(llm_provider_api_key="k", llm_provider_model="m")) == "configured"

    only_key = _llm_provider_status(s(llm_provider_api_key="k"))
    assert only_key == "NOT_CONFIGURED: missing LLM_PROVIDER_MODEL"
    # A key without a model is NOT a configured provider - the same
    # all-or-nothing gate `build_llm_provider` applies.
    assert "configured" != only_key

    only_model = _llm_provider_status(s(llm_provider_model="m"))
    assert only_model == "NOT_CONFIGURED: missing LLM_PROVIDER_API_KEY"

    both = _llm_provider_status(s())
    assert "LLM_PROVIDER_API_KEY" in both and "LLM_PROVIDER_MODEL" in both

    # The secret itself never appears in any of these.
    assert "k" not in only_key.replace("LLM_PROVIDER_API_KEY", "").replace("KEY", "")
