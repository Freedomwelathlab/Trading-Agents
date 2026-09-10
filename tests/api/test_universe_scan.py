"""Integration tests for the Phase 60 universe-scan endpoints, against a
real Postgres instance - these routes read `market_data_bars` and write
`universe_scans` / `universe_scan_results` plus one real `backtest_runs` row
(and its curve and trades) per symbol, so there is no meaningful unit-level
version of them. That half - normalization, the counts and the ranking in
isolation - is tests/backtesting/test_universe_scan.py's.

Reuses tests/api/test_admin.py's `db_session` / `api_client` / `_get_token`
fixtures and tests/api/test_strategy_backtests.py's `_validated_strategy` /
`_weekdays` rather than redefining any of them.

Two things ARE defined here, both for the same concrete reasons
tests/api/test_walk_forward.py gives for its own pair:

- `universe_scan_user`, because `backtest_user`'s teardown deletes backtest
  runs while `universe_scan_results.backtest_run_id` is ON DELETE RESTRICT -
  the scan rows have to come off first, and the schema genuinely refusing
  otherwise is that guarantee doing its job.
- `seeded_universe`, which seeds several symbols rather than one, because a
  scan that covers one symbol is a backtest wearing a different name.

Nothing is patched: these tests run the real orchestrator over the real
engine over real persisted bars. They therefore assert on the SHAPE and the
STRUCTURAL guarantees of a scan - one result per symbol, ranking consistent
with the per-symbol backtests it was derived from, failed symbols recorded
rather than dropped - rather than re-deriving the evaluator's crossing
semantics, which tests/backtesting/test_executor.py owns.

The database is shared with every other test module and is not reset between
tests, so nothing here asserts on an absolute row count outside the rows it
seeded itself. That matters more than usual for one case: "all ingested"
mode sees EVERY symbol any module has left in `market_data_bars`, so the
test for it asserts that its own seeded symbols were scanned, not that
nothing else was.
"""

import contextlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from apps.api.app.auth.permissions import Permission
from apps.api.app.auth.security import hash_password
from apps.api.app.backtesting.universe_scan import MAX_SCAN_SYMBOLS
from apps.api.app.db.models import (
    BacktestEquityPoint,
    BacktestRun,
    BacktestTrade,
    MarketDataBar,
    Role,
    Strategy,
    StrategyVersion,
    UniverseScan,
    UniverseScanResult,
    User,
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
from tests.api.test_strategies import VALID_DEFINITION, strategy_user
from tests.api.test_strategy_backtests import _validated_strategy, _weekdays

START_DATE = date(2026, 3, 2)
END_DATE = date(2026, 3, 20)

# Bars start well before the window so SMA(2)'s warmup has real history in
# front of it rather than eating into the measured range.
FIRST_BAR_DATE = date(2026, 2, 16)

# A symbol deliberately left with NO bars, to exercise the per-symbol FAILED
# path: scanning a list someone typed will routinely include one of these,
# and it must be recorded rather than aborting the scan.
UNINGESTED_SYMBOL = "NOSUCHSYMBOL.US"


# Three four-bar price shapes, each repeated with a drift, chosen so that
# the three symbols produce three DIFFERENT returns of which exactly two are
# positive. That matters for what this module tests: a ranking over symbols
# that all returned the same thing would be satisfied by any permutation,
# and `num_qualified` over a universe where nothing was profitable would
# never be anything but 0.
#
# Each shape is a dip followed by a run, which is what makes SMA(2)'s
# crossover fire at all - against SMA(2), `close > sma_2` reduces to
# `close > previous close`, so a rule that entered and exited had to be given
# a series that turns. The exact figures are not asserted anywhere; every
# number in the tests below is checked against the per-symbol backtests
# themselves, so these shapes only have to differ, not to hit a target.
_PRICE_SHAPES: list[tuple[list[int], int]] = [
    ([100, 96, 108, 116], 10),  # strong uptrend with pullbacks - profitable
    ([100, 97, 104, 107], 6),  # gentler uptrend - profitable, but less so
    ([200, 196, 190, 186], -10),  # downtrend - unprofitable
]


def _seed_bars(symbol: str, shape_index: int) -> list[Bar]:
    """One symbol's real bars: `_PRICE_SHAPES[shape_index]` repeated across
    every seeded weekday with its own per-cycle drift."""
    pattern, drift = _PRICE_SHAPES[shape_index % len(_PRICE_SHAPES)]
    return [
        Bar(
            symbol=symbol,
            bar_interval="1d",
            ts=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
            close=Decimal(pattern[index % 4] + drift * (index // 4)),
            source="test-universe-scan",
        )
        for index, day in enumerate(_weekdays(FIRST_BAR_DATE, END_DATE))
    ]


@contextlib.asynccontextmanager
async def universe_scan_user(session):
    """An active user holding both `strategy:manage` and `strategy:backtest`
    - the former only because these tests have to author and validate the
    strategy they then scan with.

    Teardown order is dictated by the schema, not by preference:
    `universe_scan_results.backtest_run_id` and
    `universe_scans.strategy_version_id` are both ON DELETE RESTRICT, so
    results go before backtest runs and scans go before the version. Having
    to work around that is the guarantee doing its job.
    """
    role_id = uuid.uuid4()
    user_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    session.add(
        Role(
            id=role_id,
            name=f"test-universescan-{role_id}",
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
        scan_ids = select(UniverseScan.id).where(
            UniverseScan.strategy_version_id.in_(version_ids)
        )
        run_ids = select(BacktestRun.id).where(BacktestRun.strategy_version_id.in_(version_ids))
        await session.execute(
            delete(UniverseScanResult).where(UniverseScanResult.universe_scan_id.in_(scan_ids))
        )
        await session.execute(
            delete(UniverseScan).where(UniverseScan.strategy_version_id.in_(version_ids))
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
async def seeded_universe(session, count: int = 2):
    """`count` unique symbols with real bars in `market_data_bars`, written
    through MarketDataStore exactly as a backfill would - the routes read the
    persisted store and nothing else, so there is no way to hand them a bar
    that was not really stored."""
    prefix = f"USQA{uuid.uuid4().hex[:6].upper()}"
    symbols = [f"{prefix}{index}.US" for index in range(count)]
    store = MarketDataStore(session)
    for index, symbol in enumerate(symbols):
        await store.upsert_bars(_seed_bars(symbol, index))
    await session.commit()
    try:
        yield symbols
    finally:
        await session.rollback()
        await session.execute(delete(MarketDataBar).where(MarketDataBar.symbol.in_(symbols)))
        await session.commit()


def _body(symbols, **overrides) -> dict:
    body = {
        "bar_interval": "1d",
        "start_date": START_DATE.isoformat(),
        "end_date": END_DATE.isoformat(),
        "starting_cash": "10000",
        **overrides,
    }
    if symbols is not None:
        body["symbols"] = symbols
    return body


@pytest.mark.asyncio
async def test_holding_strategy_manage_without_strategy_backtest_is_403() -> None:
    """The universe-scan surface is gated on `strategy:backtest`, the same
    permission the persisted-backtest and walk-forward surfaces use - so
    `strategy_user`, which holds only `strategy:manage`, is refused at the
    router's own dependency on all three routes."""
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)

            created = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/universe-scans",
                headers=headers,
                json=_body(["ANY.US"]),
            )
            listing = await client.get(
                f"/strategies/{strategy_id}/versions/{version_id}/universe-scans",
                headers=headers,
            )
            by_id = await client.get(f"/universe-scans/{uuid.uuid4()}", headers=headers)

    assert created.status_code == 403
    assert listing.status_code == 403
    assert by_id.status_code == 403


@pytest.mark.asyncio
async def test_a_user_with_no_permissions_at_all_is_403_at_the_router_level() -> None:
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.get(
                f"/universe-scans/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_an_explicit_scan_persists_ranked_results_and_is_readable_three_ways() -> None:
    """The whole happy path in one test, because the three routes describe
    the same row and asserting they agree is the point.

    Every number asserted here is checked against the PER-SYMBOL BACKTESTS it
    was derived from - each symbol's own `GET /backtest-runs/{id}` - rather
    than against a hard-coded figure, so this test cannot drift out of
    agreement with the engine it aggregates.

    One requested symbol has no bars at all, so this also pins the per-symbol
    FAILED path: recorded, unranked, metrics NULL, scan still SUCCEEDED.
    """
    async with db_session() as session, universe_scan_user(session) as (_uid, email):
        async with seeded_universe(session, 3) as symbols:
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                strategy_id, version_id = await _validated_strategy(client, token)
                requested = [*symbols, UNINGESTED_SYMBOL]

                created = await client.post(
                    f"/strategies/{strategy_id}/versions/{version_id}/universe-scans",
                    headers=headers,
                    json=_body(requested),
                )
                assert created.status_code == 201, created.text
                scan = created.json()

                assert scan["status"] == "succeeded"
                assert scan["error_detail"] is None
                assert scan["completed_at"] is not None
                assert scan["strategy_version_id"] == version_id
                assert scan["bar_interval"] == "1d"
                assert scan["scan_mode"] == "explicit_list"
                assert scan["requested_symbols"] == requested
                assert Decimal(scan["starting_cash"]) == Decimal(10_000)

                # One result per requested symbol - none dropped, none added.
                assert {row["symbol"] for row in scan["results"]} == set(requested)
                assert scan["num_symbols"] == 4
                assert scan["num_succeeded"] == 3

                by_symbol = {row["symbol"]: row for row in scan["results"]}
                gap = by_symbol[UNINGESTED_SYMBOL]
                assert gap["status"] == "failed"
                assert gap["rank"] is None
                # Never a fabricated metric for a symbol that was not measured.
                assert gap["total_return_pct"] is None
                assert gap["max_drawdown_pct"] is None
                assert gap["win_rate_pct"] is None
                assert gap["num_trades"] is None
                assert gap["error_detail"]

                # Ranked results come first, best return first, 1-indexed and
                # contiguous; the failed symbol sorts after them.
                ranked = [row for row in scan["results"] if row["rank"] is not None]
                assert [row["rank"] for row in ranked] == list(range(1, len(ranked) + 1))
                assert scan["results"][-1]["symbol"] == UNINGESTED_SYMBOL
                returns = [Decimal(row["total_return_pct"]) for row in ranked]
                assert returns == sorted(returns, reverse=True)
                # The three seeded symbols really did produce three DIFFERENT
                # returns, so the ordering above is a real ordering rather
                # than a tie any permutation would satisfy. (The tie rule
                # itself is pinned in tests/backtesting/test_universe_scan.py.)
                assert len(set(returns)) == len(returns) == 3
                # And exactly two of them were profitable, so `num_qualified`
                # is exercised as a real filter rather than as 0 or n.
                assert scan["num_qualified"] == sum(1 for value in returns if value > 0) == 2

                # Each result points at a REAL backtest run, fetchable on its
                # own - which is why the scan response inlines no equity curve.
                for row in ranked:
                    detail = await client.get(
                        f"/backtest-runs/{row['backtest_run_id']}", headers=headers
                    )
                    assert detail.status_code == 200, detail.text
                    run = detail.json()
                    assert run["status"] == "succeeded"
                    assert run["symbol"] == row["symbol"]
                    assert run["start_date"] == START_DATE.isoformat()
                    assert run["end_date"] == END_DATE.isoformat()
                    # Every symbol started fresh at the same cash - they do
                    # not share or compound a balance.
                    assert Decimal(run["starting_cash"]) == Decimal(10_000)
                    assert Decimal(run["total_return_pct"]) == Decimal(row["total_return_pct"])
                    assert run["num_trades"] == row["num_trades"]

                # The failed symbol's linked run is a real, persisted FAILED
                # backtest, not a placeholder.
                failed_detail = await client.get(
                    f"/backtest-runs/{gap['backtest_run_id']}", headers=headers
                )
                assert failed_detail.status_code == 200
                assert failed_detail.json()["status"] == "failed"

                detail = await client.get(f"/universe-scans/{scan['id']}", headers=headers)
                assert detail.status_code == 200
                assert detail.json() == scan

                listing = await client.get(
                    f"/strategies/{strategy_id}/versions/{version_id}/universe-scans",
                    headers=headers,
                )
                assert listing.status_code == 200
                listed = listing.json()
                assert listed["limit"] == 50
                assert listed["offset"] == 0
                assert [item["id"] for item in listed["items"]] == [scan["id"]]
                # A summary deliberately carries no per-symbol breakdown.
                assert "results" not in listed["items"][0]


@pytest.mark.asyncio
async def test_symbols_are_normalized_and_deduped_before_anything_runs() -> None:
    """"aapl", "AAPL " and "AAPL" are one symbol, scanned once - otherwise a
    caller could triple-count a market in `num_qualified` by typing it three
    ways."""
    async with db_session() as session, universe_scan_user(session) as (_uid, email):
        async with seeded_universe(session, 1) as symbols:
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                strategy_id, version_id = await _validated_strategy(client, token)
                symbol = symbols[0]

                created = await client.post(
                    f"/strategies/{strategy_id}/versions/{version_id}/universe-scans",
                    headers=headers,
                    json=_body([symbol.lower(), f" {symbol} ", symbol]),
                )
                assert created.status_code == 201, created.text
                scan = created.json()

    assert scan["requested_symbols"] == [symbol]
    assert scan["num_symbols"] == 1
    assert [row["symbol"] for row in scan["results"]] == [symbol]


@pytest.mark.asyncio
async def test_an_explicitly_empty_symbol_list_is_422_naming_both_options() -> None:
    """`[]` is a caller who meant something - the 422 says which two things
    they could have meant, where pydantic's generic array-length error would
    not. Nothing is persisted."""
    async with db_session() as session, universe_scan_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)
            url = f"/strategies/{strategy_id}/versions/{version_id}/universe-scans"

            response = await client.post(url, headers=headers, json=_body([]))
            listing = await client.get(url, headers=headers)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "EMPTY_SYMBOL_LIST" in detail
    assert "omit the field entirely" in detail
    assert listing.json()["items"] == []


@pytest.mark.asyncio
async def test_more_symbols_than_the_cap_is_422_naming_the_cap_and_the_count() -> None:
    """A synchronous scan is honest about what it can do in one request. The
    message quotes both numbers so the caller knows how much to trim, and no
    row is created for a request that never ran."""
    async with db_session() as session, universe_scan_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)
            url = f"/strategies/{strategy_id}/versions/{version_id}/universe-scans"
            too_many = [f"SYM{index}.US" for index in range(MAX_SCAN_SYMBOLS + 1)]

            response = await client.post(url, headers=headers, json=_body(too_many))
            listing = await client.get(url, headers=headers)

    assert len(too_many) == 51
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "TOO_MANY_SYMBOLS" in detail
    assert str(MAX_SCAN_SYMBOLS) in detail
    assert "51" in detail
    assert listing.json()["items"] == []


@pytest.mark.asyncio
async def test_all_ingested_mode_scans_the_seeded_symbols() -> None:
    """Omitting `symbols` scans whatever has actually been backfilled -
    `scan_mode` says so and `requested_symbols` is null, because the caller
    named none.

    The bar store is shared with every other test module, so this asserts
    that its OWN seeded symbols were scanned and ranked, not that nothing
    else was. If the shared store ever exceeds the cap, the route's own 422
    is the correct answer and is asserted as an acceptable outcome here
    rather than being papered over.
    """
    async with db_session() as session, universe_scan_user(session) as (_uid, email):
        async with seeded_universe(session, 2) as symbols:
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                strategy_id, version_id = await _validated_strategy(client, token)

                created = await client.post(
                    f"/strategies/{strategy_id}/versions/{version_id}/universe-scans",
                    headers=headers,
                    json=_body(None),
                )
                if created.status_code == 422:
                    assert "TOO_MANY_SYMBOLS" in created.json()["detail"]
                    pytest.skip("the shared bar store currently holds more than the scan cap")

                assert created.status_code == 201, created.text
                scan = created.json()

    assert scan["status"] == "succeeded"
    assert scan["scan_mode"] == "all_ingested"
    assert scan["requested_symbols"] is None
    scanned = {row["symbol"] for row in scan["results"]}
    assert set(symbols).issubset(scanned)
    assert scan["num_symbols"] == len(scan["results"])
    assert scan["num_succeeded"] >= len(symbols)


@pytest.mark.asyncio
async def test_scanning_with_a_draft_version_is_409_and_names_the_validate_route() -> None:
    async with db_session() as session, universe_scan_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            created = await client.post(
                "/strategies",
                headers=headers,
                json={"name": "Still a draft", "definition": VALID_DEFINITION},
            )
            assert created.status_code == 201
            strategy_id = created.json()["id"]
            version_id = created.json()["latest_version"]["id"]

            response = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/universe-scans",
                headers=headers,
                json=_body(["ANY.US"]),
            )

    assert response.status_code == 409
    assert "VERSION_NOT_VALIDATED" in response.json()["detail"]
    assert (
        f"/strategies/{strategy_id}/versions/{version_id}/validate"
        in response.json()["detail"]
    )


@pytest.mark.asyncio
async def test_another_users_strategy_and_scan_are_403() -> None:
    """Ownership is re-derived on every route, including on
    `GET /universe-scans/{id}` - holding a scan id is not authorization."""
    async with db_session() as session:
        async with universe_scan_user(session) as (_owner_id, owner_email):
            async with universe_scan_user(session) as (_other_id, other_email):
                async with seeded_universe(session, 1) as symbols:
                    async with api_client() as client:
                        owner_token = await _get_token(client, owner_email)
                        other_token = await _get_token(client, other_email)
                        other_headers = {"Authorization": f"Bearer {other_token}"}

                        strategy_id, version_id = await _validated_strategy(
                            client, owner_token
                        )
                        url = (
                            f"/strategies/{strategy_id}/versions/{version_id}/universe-scans"
                        )
                        created = await client.post(
                            url,
                            headers={"Authorization": f"Bearer {owner_token}"},
                            json=_body(symbols),
                        )
                        assert created.status_code == 201, created.text
                        scan_id = created.json()["id"]

                        assert (
                            await client.post(
                                url, headers=other_headers, json=_body(symbols)
                            )
                        ).status_code == 403
                        assert (
                            await client.get(url, headers=other_headers)
                        ).status_code == 403
                        assert (
                            await client.get(
                                f"/universe-scans/{scan_id}", headers=other_headers
                            )
                        ).status_code == 403


@pytest.mark.asyncio
async def test_unknown_ids_are_404_not_403() -> None:
    """A nonexistent id must not 403 merely because nobody can own a row that
    isn't there - the same order and codes `_load_owned_strategy` uses, which
    is exactly why that helper is imported rather than reimplemented."""
    async with db_session() as session, universe_scan_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, _version_id = await _validated_strategy(client, token)
            unknown = uuid.uuid4()

            assert (
                await client.get(f"/universe-scans/{unknown}", headers=headers)
            ).status_code == 404
            assert (
                await client.post(
                    f"/strategies/{unknown}/versions/{unknown}/universe-scans",
                    headers=headers,
                    json=_body(["ANY.US"]),
                )
            ).status_code == 404
            # The strategy exists and is the caller's; the VERSION does not.
            assert (
                await client.post(
                    f"/strategies/{strategy_id}/versions/{unknown}/universe-scans",
                    headers=headers,
                    json=_body(["ANY.US"]),
                )
            ).status_code == 404
            assert (
                await client.get(
                    f"/strategies/{strategy_id}/versions/{unknown}/universe-scans",
                    headers=headers,
                )
            ).status_code == 404


@pytest.mark.asyncio
async def test_a_malformed_request_is_422_before_anything_runs() -> None:
    """A range that does not move forward, and a non-positive
    `starting_cash`, are both malformed requests rather than empty results -
    and neither creates a row."""
    async with db_session() as session, universe_scan_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)
            url = f"/strategies/{strategy_id}/versions/{version_id}/universe-scans"

            backwards = await client.post(
                url,
                headers=headers,
                json=_body(["ANY.US"], end_date=START_DATE.isoformat()),
            )
            assert backwards.status_code == 422

            no_cash = await client.post(
                url, headers=headers, json=_body(["ANY.US"], starting_cash="0")
            )
            assert no_cash.status_code == 422

            listing = await client.get(url, headers=headers)
            assert listing.json()["items"] == []
