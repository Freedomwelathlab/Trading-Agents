"""The Phase 60 universe-scan orchestrator's own mechanics: symbol
normalization, the per-symbol result rows, the three counts, and the
read-time ranking.

**`run_strategy_backtest` is patched out in every explicit-list test here,
deliberately.** This module orchestrates the engine; it is not the engine's
test (tests/backtesting/test_engine_v2.py is, against a real Postgres).
Injecting hand-crafted per-symbol results instead makes every expected count
below hand-derivable from the injected returns alone. The DB-backed half -
real bars, real persisted rows, real ranking through the API - is
tests/api/test_universe_scan.py's job.

The session is a hand-written fake for the same reason
tests/backtesting/test_walk_forward.py uses one: `run_universe_scan` only
ever calls `add` / `add_all` / `flush` / `commit` / `refresh` on it in
explicit-list mode, so a fake records exactly what would have been written
without needing a live connection.

The ONE exception is "all ingested" mode, which resolves its universe with a
real `SELECT DISTINCT` over `market_data_bars`. That cannot be faked without
faking the very query under test, so those two tests use a real session and
real seeded bars.
"""

import contextlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import delete

from apps.api.app.backtesting.universe_scan import (
    MAX_SCAN_SYMBOLS,
    RESULT_STATUS_FAILED,
    RESULT_STATUS_SUCCEEDED,
    SCAN_MODE_ALL_INGESTED,
    SCAN_MODE_EXPLICIT_LIST,
    normalize_symbols,
    order_and_rank_results,
    resolve_scan_symbols,
    run_universe_scan,
)
from apps.api.app.db.models import (
    BacktestRunStatus,
    MarketDataBar,
    UniverseScan,
    UniverseScanResult,
    UniverseScanStatus,
)
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.store import MarketDataStore
from apps.api.app.risk.models import RiskLimits
from tests.api.test_admin import db_session

START = date(2026, 3, 2)
END = date(2026, 3, 20)


class FakeSession:
    """Records what would have been written. `flush` and `refresh` are
    no-ops because nothing here has a database to round-trip through; that
    NUMERIC(10,4) rounding really happens is the DB-backed test's assertion,
    not this module's."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def add_all(self, objs) -> None:
        self.added.extend(objs)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: object) -> None:
        return None

    @property
    def scan(self) -> UniverseScan:
        scans = [obj for obj in self.added if isinstance(obj, UniverseScan)]
        assert len(scans) == 1
        return scans[0]

    @property
    def results(self) -> list[UniverseScanResult]:
        return [obj for obj in self.added if isinstance(obj, UniverseScanResult)]


def _risk_limits() -> RiskLimits:
    """Never actually consulted - `run_strategy_backtest` is patched out -
    but passed through as the real type so the call signature is exercised
    exactly as the route builds it."""
    return RiskLimits(
        max_position_pct_of_equity=Decimal("0.30"),
        max_portfolio_exposure_pct_of_equity=Decimal(1),
        max_risk_pct_of_equity_per_trade=Decimal("0.01"),
        require_stop_price=False,
        max_market_data_age_seconds=86_400,
        duplicate_order_window_seconds=5,
    )


def _fake_backtest_run(total_return_pct: str | None, *, error: str | None = None):
    """A stand-in for one committed `BacktestRun`. `total_return_pct=None`
    means that symbol's own backtest FAILED, which is exactly the case the
    counts must skip rather than treat as 0%."""
    failed = total_return_pct is None
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=BacktestRunStatus.FAILED if failed else BacktestRunStatus.SUCCEEDED,
        total_return_pct=None if failed else Decimal(total_return_pct),
        max_drawdown_pct=None if failed else Decimal("-2.5000"),
        win_rate_pct=None if failed else Decimal("50.0000"),
        num_trades=None if failed else 2,
        error_detail=error if failed else None,
    )


async def _run(symbols, returns: list[str | None]):
    """Runs the orchestrator with one injected per-symbol result per symbol,
    in order, and returns `(session, scan, fake)`."""
    session = FakeSession()
    results = [_fake_backtest_run(value, error="no bars ingested") for value in returns]
    with patch(
        "apps.api.app.backtesting.universe_scan.run_strategy_backtest",
        side_effect=list(results),
    ) as fake:
        scan = await run_universe_scan(
            session=session,  # type: ignore[arg-type]
            strategy_version=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            symbols=symbols,
            bar_interval="1d",
            start_date=START,
            end_date=END,
            starting_cash=Decimal(10_000),
            bar_provider=SimpleNamespace(),  # type: ignore[arg-type]
            risk_limits=_risk_limits(),
            portfolio_limits=None,
            requested_by_user_id=None,
        )
    return session, scan, fake


def test_symbols_are_trimmed_upper_cased_and_deduped_in_order() -> None:
    """The same trim-and-upper-case rule watchlists apply, plus
    de-duplication - scanning one symbol twice would run its backtest twice
    and count it twice in every aggregate. Order is preserved so
    `requested_symbols` reads back as what the caller typed."""
    assert normalize_symbols([" aapl ", "MSFT", "aapl", "AAPL", "tsla"]) == [
        "AAPL",
        "MSFT",
        "TSLA",
    ]
    # Blank and whitespace-only entries contribute no symbol at all rather
    # than an empty-string one.
    assert normalize_symbols(["", "   ", "nvda"]) == ["NVDA"]
    assert normalize_symbols([]) == []


@pytest.mark.asyncio
async def test_counts_over_a_mixed_universe_of_three_symbols() -> None:
    """Two symbols succeeded (one profitable, one not) and one failed:
    3 scanned, 2 succeeded, 1 qualified. The failed symbol contributes
    NOTHING - not a zero - to `num_qualified`, and is still counted in
    `num_symbols` so the gap stays visible."""
    session, scan, fake = await _run(["aapl", "MSFT", "NOBARS"], ["12.5", "-4.25", None])

    assert scan.status is UniverseScanStatus.SUCCEEDED
    assert scan.error_detail is None
    assert scan.completed_at is not None
    assert scan.num_symbols == 3
    assert scan.num_succeeded == 2
    assert scan.num_qualified == 1

    # The universe really was normalized before anything ran, and the engine
    # was called once per symbol with the normalized name.
    assert scan.scan_mode == SCAN_MODE_EXPLICIT_LIST
    assert scan.requested_symbols == ["AAPL", "MSFT", "NOBARS"]
    assert [call.kwargs["symbol"] for call in fake.call_args_list] == [
        "AAPL",
        "MSFT",
        "NOBARS",
    ]
    # Every symbol runs the SAME window at the SAME cash - they do not share
    # or compound a balance.
    assert all(call.kwargs["start_date"] == START for call in fake.call_args_list)
    assert all(call.kwargs["end_date"] == END for call in fake.call_args_list)
    assert all(
        call.kwargs["starting_cash"] == Decimal(10_000) for call in fake.call_args_list
    )

    rows = {row.symbol: row for row in session.results}
    assert set(rows) == {"AAPL", "MSFT", "NOBARS"}
    assert rows["AAPL"].status == RESULT_STATUS_SUCCEEDED
    assert rows["AAPL"].total_return_pct == Decimal("12.5")
    assert rows["AAPL"].num_trades == 2
    assert rows["AAPL"].error_detail is None
    # A failed symbol's metrics are NULL, never 0, and it links its own real
    # failed backtest run.
    assert rows["NOBARS"].status == RESULT_STATUS_FAILED
    assert rows["NOBARS"].total_return_pct is None
    assert rows["NOBARS"].max_drawdown_pct is None
    assert rows["NOBARS"].win_rate_pct is None
    assert rows["NOBARS"].num_trades is None
    assert rows["NOBARS"].error_detail == "no bars ingested"
    assert rows["NOBARS"].backtest_run_id is not None


@pytest.mark.asyncio
async def test_a_scan_where_every_symbol_failed_still_succeeds() -> None:
    """"None of these symbols have enough history for this window" is a real,
    informative result, not an error - so the scan is SUCCEEDED with
    `num_succeeded == 0` and every symbol recorded. Zero is a genuine count
    here, unlike a zero RETURN, which would be a fabricated measurement."""
    session, scan, _fake = await _run(["A.US", "B.US"], [None, None])

    assert scan.status is UniverseScanStatus.SUCCEEDED
    assert scan.error_detail is None
    assert scan.num_symbols == 2
    assert scan.num_succeeded == 0
    assert scan.num_qualified == 0
    assert len(session.results) == 2
    assert all(row.status == RESULT_STATUS_FAILED for row in session.results)
    assert all(row.total_return_pct is None for row in session.results)


@pytest.mark.asyncio
async def test_a_zero_return_symbol_is_succeeded_but_not_qualified() -> None:
    """`num_qualified` is strictly `> 0`. A flat symbol was measured and
    produced a real result, so it counts as succeeded - it just did not make
    money, which is exactly what "qualified" claims and all it claims."""
    _session, scan, _fake = await _run(["FLAT.US", "UP.US"], ["0", "0.0001"])

    assert scan.num_succeeded == 2
    assert scan.num_qualified == 1


@pytest.mark.asyncio
async def test_an_unexpected_failure_resolves_the_row_instead_of_raising() -> None:
    """Nothing outside a symbol's own replay is expected to fail - the engine
    absorbs those into FAILED runs - but if something does, the already-created
    scan row is resolved to FAILED rather than left stranded in RUNNING by a
    500. The counts stay NULL, never 0: nothing was counted."""
    session = FakeSession()
    with patch(
        "apps.api.app.backtesting.universe_scan.run_strategy_backtest",
        side_effect=RuntimeError("bar store exploded"),
    ):
        scan = await run_universe_scan(
            session=session,  # type: ignore[arg-type]
            strategy_version=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            symbols=["AAPL"],
            bar_interval="1d",
            start_date=START,
            end_date=END,
            starting_cash=Decimal(10_000),
            bar_provider=SimpleNamespace(),  # type: ignore[arg-type]
            risk_limits=_risk_limits(),
            portfolio_limits=None,
            requested_by_user_id=None,
        )

    assert scan.status is UniverseScanStatus.FAILED
    assert "bar store exploded" in scan.error_detail
    assert scan.num_symbols is None
    assert scan.num_succeeded is None
    assert scan.num_qualified is None
    assert session.results == []


def _result(symbol: str, total_return_pct: str | None, *, failed: bool = False):
    return UniverseScanResult(
        id=uuid.uuid4(),
        universe_scan_id=uuid.uuid4(),
        symbol=symbol,
        backtest_run_id=uuid.uuid4(),
        status=RESULT_STATUS_FAILED if failed else RESULT_STATUS_SUCCEEDED,
        total_return_pct=None if total_return_pct is None else Decimal(total_return_pct),
    )


def test_rank_is_by_return_descending_with_failed_symbols_unranked() -> None:
    """Succeeded symbols rank 1..n best-first; a failed symbol gets
    `rank=None` and sorts after all of them. It was not measured and did not
    come last - ranking it would assert an ordering between a real result and
    a missing one."""
    ordered = order_and_rank_results(
        [
            _result("MID.US", "3.0"),
            _result("GONE.US", None, failed=True),
            _result("BEST.US", "9.5"),
            _result("WORST.US", "-7.25"),
        ]
    )

    assert [(row.symbol, rank) for row, rank in ordered] == [
        ("BEST.US", 1),
        ("MID.US", 2),
        ("WORST.US", 3),
        ("GONE.US", None),
    ]


def test_rank_ties_break_on_symbol_and_a_null_return_is_never_ranked() -> None:
    """Ties break on `symbol` so repeat requests and paging are
    deterministic. A 'succeeded' row with no return - structurally possible
    because the column is nullable - is sorted with the unranked group rather
    than compared against a substituted number."""
    ordered = order_and_rank_results(
        [
            _result("BBB.US", "5.0"),
            _result("AAA.US", "5.0"),
            _result("ODD.US", None),
        ]
    )

    assert [(row.symbol, rank) for row, rank in ordered] == [
        ("AAA.US", 1),
        ("BBB.US", 2),
        ("ODD.US", None),
    ]


def test_the_cap_is_a_stated_number_the_route_can_quote() -> None:
    """The cap lives in the orchestrator module, not in the route, so the
    422 the route raises and the limit the orchestrator was built for are the
    same number."""
    assert MAX_SCAN_SYMBOLS == 50


@contextlib.asynccontextmanager
async def _seeded_symbols(session, count: int):
    """Real bars for `count` unique symbols, written through MarketDataStore
    exactly as a backfill would. "All ingested" mode resolves against the
    persisted store and nothing else, so there is no way to hand it a symbol
    that was not really stored."""
    prefix = f"USQA{uuid.uuid4().hex[:6].upper()}"
    symbols = [f"{prefix}{index}.US" for index in range(count)]
    await MarketDataStore(session).upsert_bars(
        [
            Bar(
                symbol=symbol,
                bar_interval="1d",
                ts=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
                close=Decimal(100),
                source="test-universe-scan",
            )
            for symbol in symbols
            for day in (date(2026, 3, 2), date(2026, 3, 3))
        ]
    )
    await session.commit()
    try:
        yield sorted(symbols)
    finally:
        await session.rollback()
        await session.execute(delete(MarketDataBar).where(MarketDataBar.symbol.in_(symbols)))
        await session.commit()


@pytest.mark.asyncio
async def test_all_ingested_mode_resolves_every_stored_symbol_in_order() -> None:
    """`symbols=None` means "whatever has actually been backfilled" - a real
    `SELECT DISTINCT` over `market_data_bars`, in symbol order, and only for
    the requested interval. This is the one part of the orchestrator that
    cannot be faked without faking the query under test."""
    async with db_session() as session:
        async with _seeded_symbols(session, 3) as symbols:
            resolved = await resolve_scan_symbols(session, None, "1d")
            assert set(symbols).issubset(set(resolved))
            # Ordered by symbol, so the universe is stable between requests.
            assert resolved == sorted(resolved)
            # The database is shared with every other test module, so this
            # asserts on the seeded symbols' presence and ordering rather
            # than on an absolute count.

            # A different interval has none of them - nothing ingests 5m.
            other_interval = await resolve_scan_symbols(session, None, "5m")
            assert not set(symbols) & set(other_interval)


@pytest.mark.asyncio
async def test_an_explicit_list_is_resolved_without_touching_the_bar_store() -> None:
    """A symbol is accepted whether or not any bars exist for it - the same
    posture `POST /watchlists/{id}/items` takes. An unknown symbol produces a
    real FAILED result row saying exactly that, which is more useful than
    refusing the whole scan."""
    async with db_session() as session:
        assert await resolve_scan_symbols(session, [" nosuch.us ", "NOSUCH.US"], "1d") == [
            "NOSUCH.US"
        ]


@pytest.mark.asyncio
async def test_all_ingested_mode_records_no_requested_symbols() -> None:
    """`scan_mode="all_ingested"` leaves `requested_symbols` NULL rather than
    copying the resolved universe into it: the caller named no symbols, and
    `[]` would read as "asked for none". What was scanned is what the result
    rows enumerate."""
    async with db_session() as session:
        async with _seeded_symbols(session, 2) as symbols:
            resolved = await resolve_scan_symbols(session, None, "1d")

    fake = FakeSession()
    with patch(
        "apps.api.app.backtesting.universe_scan.resolve_scan_symbols",
        return_value=resolved,
    ), patch(
        "apps.api.app.backtesting.universe_scan.run_strategy_backtest",
        side_effect=[_fake_backtest_run("1.0") for _ in resolved],
    ):
        scan = await run_universe_scan(
            session=fake,  # type: ignore[arg-type]
            strategy_version=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            symbols=None,
            bar_interval="1d",
            start_date=START,
            end_date=END,
            starting_cash=Decimal(10_000),
            bar_provider=SimpleNamespace(),  # type: ignore[arg-type]
            risk_limits=_risk_limits(),
            portfolio_limits=None,
            requested_by_user_id=None,
        )

    assert scan.scan_mode == SCAN_MODE_ALL_INGESTED
    assert scan.requested_symbols is None
    assert scan.num_symbols == len(resolved)
    assert set(symbols).issubset({row.symbol for row in fake.results})
