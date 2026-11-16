"""Strategy Library routes (Phase 108, D135): permission-gated, computed on
request from a stubbed vendor, and a vendor or data problem is a coded
refusal - never an empty or invented result."""

from datetime import date, timedelta

import pytest

from apps.api.app.api.routes.library import get_library_provider
from apps.api.app.auth.permissions import Permission
from apps.api.app.library import service as library_service
from apps.api.app.main import app
from apps.api.app.marketdata.provider import DataUnavailableError
from apps.api.app.marketdata.providers.yahoo import DailyHistory
from tests.api.test_admin import _get_token, api_client, db_session, non_admin_user
from tests.api.test_deployments import _h, _role_user


def _history(symbol: str, daily: float, n: int = 900) -> DailyHistory:
    days, d = [], date(2015, 1, 5)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    closes = tuple(100.0 * (1 + daily) ** k for k in range(n))
    return DailyHistory(symbol, symbol, "USD", tuple(days), closes, closes)


class FakeVendor:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_daily_history(self, symbol, *, start, end):
        self.calls.append(symbol)
        if symbol == "GONE.US":
            raise DataUnavailableError("no such symbol")
        return _history(symbol, 0.0005 if symbol != "B.US" else 0.001)


@pytest.fixture
def vendor():
    fake = FakeVendor()
    library_service.reset_caches()
    app.dependency_overrides[get_library_provider] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_library_provider, None)
    library_service.reset_caches()


def backtester(session):
    return _role_user(
        session, permissions=[Permission.STRATEGY_BACKTEST.value], label="libbacktest"
    )


@pytest.mark.asyncio
async def test_the_library_needs_the_backtest_permission(vendor) -> None:
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r1 = await client.get("/library/strategies", headers=_h(token))
            r2 = await client.post(
                "/library/backtest",
                headers=_h(token),
                json={"strategy_id": "trend_200d", "symbols": ["A.US"]},
            )
            r3 = await client.get("/library/strategies")
    assert r1.status_code == 403 and r2.status_code == 403
    assert r3.status_code == 401
    assert vendor.calls == []


@pytest.mark.asyncio
async def test_the_catalogue_lists_strategies_with_evidence_and_universes(vendor) -> None:
    async with db_session() as session, backtester(session) as (_uid, email):
        async with api_client() as client:
            r = await client.get("/library/strategies", headers=_h(await _get_token(client, email)))
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {s["id"] for s in body["strategies"]}
    assert {
        "trend_200d",
        "tsmom_12m",
        "dual_momentum",
        "momentum_rotation",
        "rsi2_reversion",
        "donchian_breakout",
    } <= ids
    assert all(s["evidence"] and s["caveats"] for s in body["strategies"])
    markets = {p["market"] for p in body["presets"]}
    assert {"US", "SG", "IN", "Crypto", "FX"} <= markets
    assert body["cost_bps"]["CRYPTO"] > body["cost_bps"]["US"]
    assert "not advice" in body["disclaimer"]


@pytest.mark.asyncio
async def test_a_backtest_reports_segments_a_verdict_and_what_to_hold(vendor) -> None:
    async with db_session() as session, backtester(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(
                "/library/backtest",
                headers=_h(token),
                json={"strategy_id": "trend_200d", "symbols": ["a.us"], "benchmark": ["A.US"]},
            )
            again = await client.post(
                "/library/backtest",
                headers=_h(token),
                json={"strategy_id": "trend_200d", "symbols": ["a.us"], "benchmark": ["A.US"]},
            )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbols"] == ["A.US"] and body["source"] == "yahoo"
    assert body["segments"][0]["label"] == "Whole period"
    assert body["segments"][0]["strategy"]["cagr"] is not None
    assert body["verdict"]["code"] in {"beats_benchmark", "lower_risk", "no_edge"}
    assert body["target"] == [{"symbol": "A.US", "weight": 1.0, "note": body["target"][0]["note"]}]
    assert 2 <= len(body["curve"]) <= 261
    assert body["data"][0]["bars"] == 900
    assert again.status_code == 200
    assert vendor.calls == ["A.US"]  # the second request was served from cache


@pytest.mark.asyncio
async def test_a_universe_supplies_symbols_and_its_warnings(vendor) -> None:
    async with db_session() as session, backtester(session) as (_uid, email):
        async with api_client() as client:
            r = await client.post(
                "/library/backtest",
                headers=_h(await _get_token(client, email)),
                json={"strategy_id": "momentum_rotation", "preset_id": "sg_bluechips"},
            )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "D05.SG" in body["symbols"] and body["benchmark_symbols"] == ["ES3.SG"]
    assert any("Survivorship" in w for w in body["warnings"])
    assert body["verdict"]["code"] != "beats_benchmark"


@pytest.mark.asyncio
async def test_refusals_carry_a_code(vendor) -> None:
    async with db_session() as session, backtester(session) as (_uid, email):
        async with api_client() as client:
            h = _h(await _get_token(client, email))
            gone = await client.post(
                "/library/backtest",
                headers=h,
                json={"strategy_id": "trend_200d", "symbols": ["GONE.US"]},
            )
            unknown = await client.post(
                "/library/backtest",
                headers=h,
                json={"strategy_id": "nope", "symbols": ["A.US"]},
            )
            bad_param = await client.post(
                "/library/backtest",
                headers=h,
                json={"strategy_id": "trend_200d", "symbols": ["A.US"], "params": {"sma_days": 1}},
            )
            too_few = await client.post(
                "/library/backtest",
                headers=h,
                json={"strategy_id": "dual_momentum", "symbols": ["A.US"]},
            )
            nothing = await client.post(
                "/library/backtest",
                headers=h,
                json={"strategy_id": "trend_200d"},
            )
            bad_preset = await client.post(
                "/library/backtest",
                headers=h,
                json={"strategy_id": "trend_200d", "preset_id": "nope"},
            )
    assert gone.status_code == 503 and "DATA_UNAVAILABLE" in gone.json()["detail"]
    assert unknown.status_code == 404
    assert bad_param.status_code == 422 and "between" in bad_param.json()["detail"]
    assert too_few.status_code == 422 and "at least 2" in too_few.json()["detail"]
    assert nothing.status_code == 422
    assert bad_preset.status_code == 404
