"""Integration tests for POST /backtests (docs/DECISIONS.md D025) against a
real Postgres instance (for auth/session wiring, even though the backtest
itself never touches broker_accounts/broker_positions rows) and a fake
HistoryProvider (no real Longbridge credentials needed). Reuses
tests/api/test_trades.py's fixtures for consistency with the rest of the
suite.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from apps.api.app.api.dependencies import get_history_provider
from apps.api.app.main import app
from apps.api.app.marketdata.provider import VendorError
from tests.api.test_trades import _get_token as get_token
from tests.api.test_trades import active_user, api_client, db_session


class FakeHistoryProvider:
    name = "fake-history"

    def __init__(self, *, closes: list[Decimal] | None = None, error: Exception | None = None):
        self._closes = closes
        self._error = error

    async def get_daily_closes(self, symbol: str, *, count: int) -> list[Decimal]:
        if self._error is not None:
            raise self._error
        assert self._closes is not None
        return self._closes[-count:] if count <= len(self._closes) else self._closes


def _today() -> date:
    return datetime.now(UTC).date()


def _recent_weekdays(n: int) -> list[date]:
    """n most recent Mon-Fri dates, oldest-first, ending today - mirrors
    engine._label_trading_days so a test's request always lines up with
    what a real request would compute."""
    dates: list[date] = []
    current = _today()
    while len(dates) < n:
        if current.weekday() < 5:
            dates.append(current)
        current -= timedelta(days=1)
    dates.reverse()
    return dates


@pytest.mark.asyncio
async def test_requires_authentication():
    async with api_client() as client:
        response = await client.post(
            "/backtests",
            json={
                "symbol": "AAPL",
                "start_date": "2020-01-01",
                "end_date": "2020-01-02",
                "starting_cash": "10000",
            },
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_no_history_provider_configured_returns_not_configured():
    async with (
        db_session() as session,
        active_user(session, permissions=()) as (user_id, email),
    ):
        del user_id
        async with api_client() as client:
            app.dependency_overrides[get_history_provider] = lambda: None
            try:
                token = await get_token(client, email)
                response = await client.post(
                    "/backtests",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "start_date": "2020-01-01",
                        "end_date": "2020-01-02",
                        "starting_cash": "10000",
                    },
                )
            finally:
                del app.dependency_overrides[get_history_provider]
    assert response.status_code == 400
    assert "NOT_CONFIGURED" in response.json()["detail"]


@pytest.mark.asyncio
async def test_any_authenticated_user_can_run_a_backtest_no_special_permission_needed():
    # permissions=() -> a role that grants nothing at all - proves this
    # endpoint deliberately does not gate on a specific Permission
    # (docs/DECISIONS.md D025).
    window_dates = _recent_weekdays(3)
    warmup = [Decimal(100)] * 20
    window = [Decimal(101), Decimal(99), Decimal(102)]
    fake_history = FakeHistoryProvider(closes=warmup + window)

    async with (
        db_session() as session,
        active_user(session, permissions=()) as (user_id, email),
    ):
        del user_id
        async with api_client() as client:
            app.dependency_overrides[get_history_provider] = lambda: fake_history
            try:
                token = await get_token(client, email)
                response = await client.post(
                    "/backtests",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "start_date": window_dates[0].isoformat(),
                        "end_date": window_dates[-1].isoformat(),
                        "starting_cash": "10000",
                    },
                )
            finally:
                del app.dependency_overrides[get_history_provider]

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["symbol"] == "AAPL"
    assert body["starting_cash"] == "10000"
    assert len(body["equity_curve"]) == 3
    assert [p["date"] for p in body["equity_curve"]] == [d.isoformat() for d in window_dates]


@pytest.mark.asyncio
async def test_insufficient_history_returns_data_unavailable():
    fake_history = FakeHistoryProvider(closes=[Decimal(100)] * 5)
    window_dates = _recent_weekdays(3)

    async with (
        db_session() as session,
        active_user(session, permissions=()) as (user_id, email),
    ):
        del user_id
        async with api_client() as client:
            app.dependency_overrides[get_history_provider] = lambda: fake_history
            try:
                token = await get_token(client, email)
                response = await client.post(
                    "/backtests",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "start_date": window_dates[0].isoformat(),
                        "end_date": window_dates[-1].isoformat(),
                        "starting_cash": "10000",
                    },
                )
            finally:
                del app.dependency_overrides[get_history_provider]

    assert response.status_code == 400
    assert "DATA_UNAVAILABLE" in response.json()["detail"]


@pytest.mark.asyncio
async def test_end_date_not_today_is_rejected_with_a_clear_400():
    fake_history = FakeHistoryProvider(closes=[Decimal(100)] * 30)

    async with (
        db_session() as session,
        active_user(session, permissions=()) as (user_id, email),
    ):
        del user_id
        async with api_client() as client:
            app.dependency_overrides[get_history_provider] = lambda: fake_history
            try:
                token = await get_token(client, email)
                response = await client.post(
                    "/backtests",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "start_date": "2020-01-01",
                        "end_date": "2020-01-05",
                        "starting_cash": "10000",
                    },
                )
            finally:
                del app.dependency_overrides[get_history_provider]

    assert response.status_code == 400
    assert "UNSUPPORTED_DATE_RANGE" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_failing_vendor_returns_a_502_not_a_fabricated_result():
    fake_history = FakeHistoryProvider(error=VendorError("vendor down"))
    window_dates = _recent_weekdays(3)

    async with (
        db_session() as session,
        active_user(session, permissions=()) as (user_id, email),
    ):
        del user_id
        async with api_client() as client:
            app.dependency_overrides[get_history_provider] = lambda: fake_history
            try:
                token = await get_token(client, email)
                response = await client.post(
                    "/backtests",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "start_date": window_dates[0].isoformat(),
                        "end_date": window_dates[-1].isoformat(),
                        "starting_cash": "10000",
                    },
                )
            finally:
                del app.dependency_overrides[get_history_provider]

    assert response.status_code == 502
    assert "DATA_UNAVAILABLE" in response.json()["detail"]


@pytest.mark.asyncio
async def test_end_date_before_start_date_is_a_422_validation_error():
    async with (
        db_session() as session,
        active_user(session, permissions=()) as (user_id, email),
    ):
        del user_id
        async with api_client() as client:
            token = await get_token(client, email)
            response = await client.post(
                "/backtests",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "symbol": "AAPL",
                    "start_date": "2020-01-05",
                    "end_date": "2020-01-01",
                    "starting_cash": "10000",
                },
            )
    assert response.status_code == 422
