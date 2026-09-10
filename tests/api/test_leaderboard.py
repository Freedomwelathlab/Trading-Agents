"""Integration tests for the Phase 59 strategy leaderboard, against a real
Postgres instance.

Nothing is mocked and nothing is inserted by hand: every score asserted below
is computed from rows that four earlier phases' REAL routes wrote during the
test - `POST .../backtests` (Phase 55), `POST .../walk-forward` (Phase 57) and
`POST .../robustness` (Phase 58), each running its real engine over really
persisted bars. That is the point of this module. The leaderboard is a
read-model over four tables, so the only way to know it reads them correctly
is to make the four tables real; the model's own arithmetic, in isolation, is
tests/strategies/test_scoring.py's.

Reuses tests/api/test_admin.py's `db_session` / `api_client` / `_get_token` /
`non_admin_user`, tests/api/test_strategies.py's `strategy_user`, and
tests/api/test_strategy_backtests.py's `_weekdays` and `_validated_strategy`
rather than redefining any of them.

Two things ARE defined here, each for a concrete reason:

- `leaderboard_user`, following tests/api/test_robustness.py's fixture
  exactly. Its teardown order is dictated by the schema rather than by
  preference: `walk_forward_windows.backtest_run_id` and every
  `*_runs.strategy_version_id` are ON DELETE RESTRICT, so windows come off
  before backtest runs, and the analysis runs come off before the version.
  Having to work around that is those guarantees doing their job.
- `TRENDING_UP` / `TRENDING_DOWN` close series. A leaderboard test needs one
  strategy that genuinely holds up out of sample and one that genuinely does
  not, and that cannot be arranged by asserting harder - it has to come out
  of the bars. Both series are a 10-day sawtooth (five rising days, five
  falling) so the crossover rules really fire; the only difference is the
  drift between cycles, which is what makes one strategy profitable in most
  walk-forward windows and the other in none.

The database is shared with every other test module and is not reset between
tests, so nothing here asserts on an absolute row count outside the rows it
seeded itself.
"""

import contextlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from apps.api.app.auth.permissions import Permission
from apps.api.app.auth.security import hash_password
from apps.api.app.db.models import (
    BacktestEquityPoint,
    BacktestRun,
    BacktestTrade,
    MarketDataBar,
    RobustnessPerturbationResult,
    RobustnessRun,
    Role,
    Strategy,
    StrategyVersion,
    User,
    WalkForwardRun,
    WalkForwardWindow,
)
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.store import MarketDataStore
from tests.api.test_admin import (
    TEST_PASSWORD,
    _get_token,
    api_client,
    db_session,
    non_admin_user,
)
from tests.api.test_strategies import strategy_user
from tests.api.test_strategy_backtests import _validated_strategy, _weekdays

SMA10_DEFINITION = {
    "indicators": [{"id": "sma_10", "type": "sma", "period": 10}],
    "entry_rule": {"op": "crosses_above", "left": "close", "right": "sma_10"},
    "exit_rule": {"op": "crosses_below", "left": "close", "right": "sma_10"},
    "position_sizing": {"type": "all_in"},
}
"""Period 10 for the reason tests/api/test_robustness.py gives: a +/-10%
nudge lands on 11 and 9, two genuinely different definitions, so the
robustness run this module needs really has something to perturb."""

FIRST_BAR_DATE = date(2026, 1, 5)
"""Bars start almost two months early so every walk-forward window AND every
perturbed (longer-period) variant finds its own full warmup."""

WINDOW_START = date(2026, 3, 2)
WINDOW_END = date(2026, 4, 12)
WALK_FORWARD_DAYS = 14
"""42 calendar days sliced into three exact 14-day windows - no partial tail
is involved, so what this module exercises is the leaderboard rather than the
boundary rule tests/backtesting/test_walk_forward.py already pins."""
EXPECTED_WINDOWS = 3
STARTING_CASH = Decimal(10_000)


def _sawtooth(base: int, index: int) -> int:
    """A 10-bar cycle: five rising days then five falling, around `base`.

    The oscillation is what makes the crossover rules fire at all - a
    monotonic series would never cross its own moving average - and the
    caller's `base`, which steps between cycles, is what decides whether the
    strategy makes money doing so.
    """
    position = index % 10
    return base + (position * 6 if position < 5 else 24 - (position - 5) * 5)


def _trending_up(index: int) -> int:
    return _sawtooth(100 + (index // 10) * 10, index)


def _trending_down(index: int) -> int:
    return _sawtooth(300 - (index // 10) * 10, index)


def _seed_bars(symbol: str, closes) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            bar_interval="1d",
            ts=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
            close=Decimal(closes(index)),
            source="test-leaderboard",
        )
        for index, day in enumerate(_weekdays(FIRST_BAR_DATE, WINDOW_END))
    ]


@contextlib.asynccontextmanager
async def leaderboard_user(session):
    """An active user holding `strategy:manage` AND `strategy:backtest`.

    `strategy:backtest` is needed only to CREATE the runs these tests then
    rank - the leaderboard route itself is gated on `strategy:manage` alone,
    which `test_a_manage_only_user_can_read_the_leaderboard` below proves by
    reading it as a user who holds nothing else.

    Teardown order is the schema's, not a preference: walk-forward windows
    reference backtest runs ON DELETE RESTRICT, and every analysis run
    references the strategy version the same way.
    """
    role_id = uuid.uuid4()
    user_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    session.add(
        Role(
            id=role_id,
            name=f"test-leaderboard-{role_id}",
            permissions=[
                Permission.STRATEGY_MANAGE.value,
                Permission.STRATEGY_BACKTEST.value,
            ],
        )
    )
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
        run_ids = select(BacktestRun.id).where(BacktestRun.strategy_version_id.in_(version_ids))
        walk_forward_ids = select(WalkForwardRun.id).where(
            WalkForwardRun.strategy_version_id.in_(version_ids)
        )
        robustness_ids = select(RobustnessRun.id).where(
            RobustnessRun.strategy_version_id.in_(version_ids)
        )
        await session.execute(
            delete(WalkForwardWindow).where(
                WalkForwardWindow.walk_forward_run_id.in_(walk_forward_ids)
            )
        )
        await session.execute(
            delete(WalkForwardRun).where(WalkForwardRun.strategy_version_id.in_(version_ids))
        )
        await session.execute(
            delete(RobustnessPerturbationResult).where(
                RobustnessPerturbationResult.robustness_run_id.in_(robustness_ids)
            )
        )
        await session.execute(
            delete(RobustnessRun).where(RobustnessRun.strategy_version_id.in_(version_ids))
        )
        await session.execute(
            delete(BacktestTrade).where(BacktestTrade.backtest_run_id.in_(run_ids))
        )
        await session.execute(
            delete(BacktestEquityPoint).where(BacktestEquityPoint.backtest_run_id.in_(run_ids))
        )
        await session.execute(
            delete(BacktestRun).where(BacktestRun.strategy_version_id.in_(version_ids))
        )
        await session.execute(delete(Strategy).where(Strategy.owner_user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Role).where(Role.id == role_id))
        await session.commit()


@contextlib.asynccontextmanager
async def seeded_symbol(session, closes):
    """A unique symbol with real bars in `market_data_bars`, written through
    MarketDataStore exactly as a backfill would - the engines read the
    persisted store and nothing else, so there is no way to hand them a bar
    that was not really stored."""
    symbol = f"LBQA{uuid.uuid4().hex[:8].upper()}.US"
    await MarketDataStore(session).upsert_bars(_seed_bars(symbol, closes))
    await session.commit()
    try:
        yield symbol
    finally:
        await session.rollback()
        await session.execute(delete(MarketDataBar).where(MarketDataBar.symbol == symbol))
        await session.commit()


async def _named_strategy(client, token, name: str) -> tuple[str, str]:
    """A validated SMA(10) strategy with a distinct name, so the leaderboard's
    `strategy_name` field is asserted against something meaningful. Renamed
    through the real `PATCH /strategies/{id}` rather than by touching the row
    directly."""
    headers = {"Authorization": f"Bearer {token}"}
    strategy_id, version_id = await _validated_strategy(
        client, token, definition=SMA10_DEFINITION
    )
    renamed = await client.patch(f"/strategies/{strategy_id}", headers=headers, json={"name": name})
    assert renamed.status_code == 200, renamed.text
    return strategy_id, version_id


async def _backtest(client, token, strategy_id, version_id, symbol) -> dict:
    response = await client.post(
        f"/strategies/{strategy_id}/versions/{version_id}/backtests",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "symbol": symbol,
            "bar_interval": "1d",
            "start_date": WINDOW_START.isoformat(),
            "end_date": WINDOW_END.isoformat(),
            "starting_cash": str(STARTING_CASH),
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "succeeded", response.text
    return response.json()


async def _walk_forward(client, token, strategy_id, version_id, symbol) -> dict:
    response = await client.post(
        f"/strategies/{strategy_id}/versions/{version_id}/walk-forward",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "symbol": symbol,
            "bar_interval": "1d",
            "overall_start_date": WINDOW_START.isoformat(),
            "overall_end_date": WINDOW_END.isoformat(),
            "window_days": WALK_FORWARD_DAYS,
            "starting_cash": str(STARTING_CASH),
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "succeeded", response.text
    return response.json()


async def _robustness(client, token, strategy_id, version_id, symbol) -> dict:
    response = await client.post(
        f"/strategies/{strategy_id}/versions/{version_id}/robustness",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "symbol": symbol,
            "bar_interval": "1d",
            "start_date": WINDOW_START.isoformat(),
            "end_date": WINDOW_END.isoformat(),
            "starting_cash": str(STARTING_CASH),
            "magnitude_pct": "10",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "succeeded", response.text
    return response.json()


def _entry(body: dict, strategy_id: str) -> dict:
    matches = [item for item in body["items"] if item["strategy_id"] == strategy_id]
    assert len(matches) == 1, f"expected {strategy_id} exactly once in {body}"
    return matches[0]


def _component(entry: dict, name: str) -> dict:
    matches = [item for item in entry["score"]["components"] if item["name"] == name]
    assert len(matches) == 1, f"expected one {name!r} component, got {matches}"
    return matches[0]


@pytest.mark.asyncio
async def test_a_user_with_no_permissions_at_all_is_403_at_the_router_level() -> None:
    """The leaderboard is gated at the router's own dependency, exactly like
    every other Strategy Lab surface - a caller without `strategy:manage`
    never reaches the handler."""
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.get(
                "/strategies/leaderboard",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_a_manage_only_user_can_read_the_leaderboard() -> None:
    """`strategy:manage` alone is sufficient, deliberately: a leaderboard of
    your own strategies is a READ over the resource that permission already
    gates, not a new capability. `strategy_user` holds nothing else and gets
    a 200 - with an empty list, because it has authored nothing."""
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.get(
                "/strategies/leaderboard",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "limit": 50, "offset": 0}


@pytest.mark.asyncio
async def test_an_empty_leaderboard_is_a_200_never_an_error() -> None:
    """A caller whose only strategy has never been backtested gets an empty
    list and a 200 - and so does a `min_status=validated` filter that nothing
    currently meets.

    "No sufficiently robust strategy found" is the honest answer the master
    spec asks for, and it is preferable to presenting a misleading strategy.
    An empty array IS that answer, so it must not be dressed up as a failure.
    """
    async with db_session() as session, leaderboard_user(session) as (_uid, email):
        async with seeded_symbol(session, _trending_up) as symbol:
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}

                # A strategy with no run of any kind: nothing to measure, so
                # it is excluded rather than ranked last with a zero.
                await _named_strategy(client, token, "Never backtested")
                unrun = await client.get("/strategies/leaderboard", headers=headers)
                assert unrun.status_code == 200, unrun.text
                assert unrun.json()["items"] == []

                # Now one WITH a backtest - it ranks, but only at
                # `insufficient_data`, so a `validated` floor still finds
                # nothing and still answers 200.
                strategy_id, version_id = await _named_strategy(
                    client, token, "Backtest only"
                )
                await _backtest(client, token, strategy_id, version_id, symbol)

                unfiltered = await client.get("/strategies/leaderboard", headers=headers)
                filtered = await client.get(
                    "/strategies/leaderboard?min_status=validated", headers=headers
                )

    assert unfiltered.status_code == 200
    assert [item["strategy_id"] for item in unfiltered.json()["items"]] == [strategy_id]
    assert unfiltered.json()["items"][0]["score"]["status"] == "insufficient_data"

    assert filtered.status_code == 200, filtered.text
    assert filtered.json() == {"items": [], "limit": 50, "offset": 0}


@pytest.mark.asyncio
async def test_the_leaderboard_ranks_scores_and_labels_real_runs() -> None:
    """The whole model over real persisted results, in one test because the
    ranking is a statement ABOUT the three strategies together.

    Three strategies, all authored by one user, differing only in how much
    evidence exists for them and what that evidence says:

    - "Validated" - backtested, walk-forward tested over three windows on a
      series that trends up between cycles, and robustness tested. Both
      checks run, neither flags.
    - "Overfit" - the same rules over a series that trends DOWN, so most
      walk-forward windows lose money. One check run, and it flags.
    - "Backtest only" - one backtest and nothing else. Not a failure; an
      absence, and it is labelled as one.
    """
    async with db_session() as session, leaderboard_user(session) as (_uid, email):
        async with seeded_symbol(session, _trending_up) as up_symbol:
            async with seeded_symbol(session, _trending_down) as down_symbol:
                async with api_client() as client:
                    token = await _get_token(client, email)
                    headers = {"Authorization": f"Bearer {token}"}

                    good_id, good_version = await _named_strategy(
                        client, token, "Validated"
                    )
                    good_backtest = await _backtest(
                        client, token, good_id, good_version, up_symbol
                    )
                    good_wf = await _walk_forward(
                        client, token, good_id, good_version, up_symbol
                    )
                    good_rb = await _robustness(
                        client, token, good_id, good_version, up_symbol
                    )

                    bad_id, bad_version = await _named_strategy(client, token, "Overfit")
                    await _backtest(client, token, bad_id, bad_version, down_symbol)
                    bad_wf = await _walk_forward(
                        client, token, bad_id, bad_version, down_symbol
                    )

                    thin_id, thin_version = await _named_strategy(
                        client, token, "Backtest only"
                    )
                    await _backtest(client, token, thin_id, thin_version, up_symbol)

                    # A fourth strategy with no runs at all - excluded
                    # entirely, never ranked last with a fabricated zero.
                    unrun_id, _unrun_version = await _named_strategy(
                        client, token, "Never backtested"
                    )

                    response = await client.get("/strategies/leaderboard", headers=headers)
                    promising_only = await client.get(
                        "/strategies/leaderboard?min_status=promising", headers=headers
                    )
                    paged = await client.get(
                        "/strategies/leaderboard?limit=1&offset=1", headers=headers
                    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["limit"] == 50
    assert body["offset"] == 0

    ranked_ids = [item["strategy_id"] for item in body["items"]]
    assert set(ranked_ids) == {good_id, bad_id, thin_id}
    assert unrun_id not in ranked_ids

    good = _entry(body, good_id)
    bad = _entry(body, bad_id)
    thin = _entry(body, thin_id)

    # --- the run data these scores claim to summarise really says this ---
    assert good_wf["num_windows"] == EXPECTED_WINDOWS
    assert good_wf["num_profitable_windows"] * 2 >= good_wf["num_windows"]
    assert bad_wf["num_windows"] == EXPECTED_WINDOWS
    assert bad_wf["num_profitable_windows"] * 2 < bad_wf["num_windows"]
    assert Decimal(good_rb["max_return_deviation_pct"]) <= Decimal(20)

    # --- statuses ---
    assert good["score"]["status"] == "validated"
    assert bad["score"]["status"] == "overfit_risk"
    assert thin["score"]["status"] == "insufficient_data"

    # --- every status names the actual numbers behind it ---
    assert (
        f"{good_wf['num_profitable_windows']}/{good_wf['num_windows']} profitable windows"
        in good["score"]["status_reason"]
    )
    assert (
        f"{bad_wf['num_profitable_windows']}/{bad_wf['num_windows']} profitable windows"
        in bad["score"]["status_reason"]
    )
    assert "unmeasured" in thin["score"]["status_reason"]

    # --- components: absent is never zero ---
    assert {item["name"] for item in good["score"]["components"]} == {
        "return",
        "risk",
        "consistency",
        "parameter_stability",
    }
    assert {item["name"] for item in bad["score"]["components"]} == {
        "return",
        "risk",
        "consistency",
    }
    assert {item["name"] for item in thin["score"]["components"]} == {"return", "risk"}
    assert good["score"]["components_measured"] == 4
    assert bad["score"]["components_measured"] == 3
    assert thin["score"]["components_measured"] == 2
    assert Decimal(good["score"]["max_possible_points"]) == Decimal(100)
    assert Decimal(bad["score"]["max_possible_points"]) == Decimal(75)
    assert Decimal(thin["score"]["max_possible_points"]) == Decimal(50)

    # --- the arithmetic is reproducible from the response alone ---
    for entry in body["items"]:
        score = entry["score"]
        component_total = sum(Decimal(item["points"]) for item in score["components"])
        assert abs(Decimal(score["total_points"]) - component_total) <= Decimal("0.0001")
        expected_pct = (
            Decimal(score["total_points"]) / Decimal(score["max_possible_points"]) * 100
        )
        assert abs(Decimal(score["percentage"]) - expected_pct) <= Decimal("0.0001")
        assert all(Decimal(item["max_points"]) == Decimal(25) for item in score["components"])

    # --- the detail strings name the real column values ---
    assert (
        f"total_return_pct={good_backtest['total_return_pct']}%"
        in _component(good, "return")["detail"]
    )
    assert (
        f"max_drawdown_pct={good_backtest['max_drawdown_pct']}%"
        in _component(good, "risk")["detail"]
    )

    # --- provenance points at the real runs ---
    assert good["score"]["latest_backtest_run_id"] == good_backtest["id"]
    # And specifically NOT at one of the walk-forward run's own per-window
    # backtest rows, which are newer than the requested backtest and carry
    # the same strategy_version_id. Scoring return/risk off a window would
    # describe a third of the range under a window nobody asked for - see
    # scoring.py::_latest_succeeded_backtest.
    window_run_ids = {window["backtest_run_id"] for window in good_wf["windows"]}
    assert len(window_run_ids) == EXPECTED_WINDOWS
    assert good["score"]["latest_backtest_run_id"] not in window_run_ids
    assert good["score"]["latest_walk_forward_run_id"] == good_wf["id"]
    assert good["score"]["latest_robustness_run_id"] == good_rb["id"]
    # No Monte Carlo run was requested for any of these.
    assert good["score"]["latest_monte_carlo_run_id"] is None
    assert thin["score"]["latest_walk_forward_run_id"] is None
    assert thin["score"]["latest_robustness_run_id"] is None

    # --- the version scored is the strategy's latest, and is named ---
    assert good["strategy_version_id"] == good_version
    assert good["strategy_version_number"] == 1
    assert good["strategy_name"] == "Validated"
    assert bad["strategy_name"] == "Overfit"
    assert thin["strategy_name"] == "Backtest only"

    # --- ranking: by percentage, descending, best first ---
    percentages = [Decimal(item["score"]["percentage"]) for item in body["items"]]
    assert percentages == sorted(percentages, reverse=True)
    assert Decimal(good["score"]["percentage"]) > Decimal(bad["score"]["percentage"])
    assert ranked_ids[0] == good_id
    assert ranked_ids[-1] == bad_id

    # --- min_status: a flagged strategy never satisfies a floor above the
    # bottom, even though it out-scores nothing here by accident ---
    assert promising_only.status_code == 200
    promising_ids = [item["strategy_id"] for item in promising_only.json()["items"]]
    assert good_id in promising_ids
    assert bad_id not in promising_ids
    assert thin_id not in promising_ids

    # --- paging slices the ranking, it does not reorder it ---
    assert paged.status_code == 200
    assert paged.json()["limit"] == 1
    assert paged.json()["offset"] == 1
    assert [item["strategy_id"] for item in paged.json()["items"]] == [ranked_ids[1]]


@pytest.mark.asyncio
async def test_another_users_strategy_never_appears_in_the_callers_leaderboard() -> None:
    """Ownership is applied in the query itself, mirroring `list_strategies` -
    there is no ordering of events in which another user's strategy is scored
    and then filtered out."""
    async with db_session() as session:
        async with leaderboard_user(session) as (_owner_id, owner_email):
            async with leaderboard_user(session) as (_other_id, other_email):
                async with seeded_symbol(session, _trending_up) as symbol:
                    async with api_client() as client:
                        owner_token = await _get_token(client, owner_email)
                        other_token = await _get_token(client, other_email)

                        owner_strategy, owner_version = await _named_strategy(
                            client, owner_token, "Owner's strategy"
                        )
                        await _backtest(
                            client, owner_token, owner_strategy, owner_version, symbol
                        )

                        owner_view = await client.get(
                            "/strategies/leaderboard",
                            headers={"Authorization": f"Bearer {owner_token}"},
                        )
                        other_view = await client.get(
                            "/strategies/leaderboard",
                            headers={"Authorization": f"Bearer {other_token}"},
                        )

    assert owner_view.status_code == 200
    assert [item["strategy_id"] for item in owner_view.json()["items"]] == [owner_strategy]

    # The other user holds the same permission and sees an empty leaderboard,
    # not a 403 - they are not forbidden, they simply own nothing.
    assert other_view.status_code == 200
    assert other_view.json()["items"] == []


@pytest.mark.asyncio
async def test_an_unknown_min_status_is_422_before_anything_is_scored() -> None:
    """`overfit_risk` is deliberately not offerable as a floor - it is a
    warning, not a rung on the ladder - and neither is a typo."""
    async with db_session() as session, leaderboard_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            assert (
                await client.get(
                    "/strategies/leaderboard?min_status=overfit_risk", headers=headers
                )
            ).status_code == 422
            assert (
                await client.get(
                    "/strategies/leaderboard?min_status=nonsense", headers=headers
                )
            ).status_code == 422
