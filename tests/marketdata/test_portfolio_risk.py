"""Tests for `apps/api/app/marketdata/portfolio_risk.py` (Phase 62, D079).

Two layers, the same split the module has:

- `compute_market_risk_inputs` is pure - hand-built `Bar` series with
  chosen closes, every statistic checked against Decimal arithmetic done
  independently in the test.
- `load_market_risk_inputs` is the thin async wrapper - exercised against a
  real isolated Postgres exactly like `tests/marketdata/test_store.py`,
  asserting it agrees with the pure function on the same bars.
"""

import contextlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete

from apps.api.app.db.base import get_session
from apps.api.app.db.models import MarketDataBar
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.portfolio_risk import (
    _log_returns_by_date,
    compute_market_risk_inputs,
    load_market_risk_inputs,
)
from apps.api.app.marketdata.store import MarketDataStore

_BASE_TS = datetime(2026, 1, 1, 21, 0, tzinfo=UTC)


def _bars(
    symbol: str,
    closes: list[str | Decimal],
    *,
    first_offset_days: int = 0,
) -> list[Bar]:
    """Daily bars, oldest-first, one per calendar day (the store contract
    only cares that the dates are distinct and ascending)."""
    out: list[Bar] = []
    for i, close in enumerate(closes):
        ts = _BASE_TS + timedelta(days=first_offset_days + i)
        out.append(
            Bar(
                symbol=symbol,
                bar_interval="1d",
                ts=ts,
                open=None,
                high=None,
                low=None,
                close=Decimal(str(close)),
                volume=1_000,
                source="test-fixture",
            )
        )
    return out


def _alternating(low: str, high: str, n: int) -> list[str]:
    """`low, high, low, high, ...` - a series whose daily log returns are
    +ln(high/low), -ln(high/low), ... : constant in magnitude, so its
    sample standard deviation is hand-computable."""
    return [low if k % 2 == 0 else high for k in range(n)]


# --------------------------------------------------------------------------
# compute_market_risk_inputs - pure
# --------------------------------------------------------------------------


def test_annualized_volatility_matches_hand_computed_stdev_times_sqrt_252():
    closes = _alternating("100", "110", 61)  # 60 daily log returns
    inputs = compute_market_risk_inputs(
        {"AAA": _bars("AAA", closes)}, min_observations=60
    )

    returns = [
        (Decimal(closes[i + 1]) / Decimal(closes[i])).ln()
        for i in range(len(closes) - 1)
    ]
    n = len(returns)
    mean = sum(returns, Decimal(0)) / n
    variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / (n - 1)
    expected = variance.sqrt() * Decimal(252).sqrt()

    assert abs(inputs.annualized_volatility["AAA"] - expected) < Decimal("1e-24")
    # And the same figure sanity-checked against the closed form for this
    # constant-magnitude series: |r| * sqrt(n/(n-1)) * sqrt(252).
    magnitude = (Decimal("110") / Decimal("100")).ln()
    closed_form = magnitude * (Decimal(n) / Decimal(n - 1)).sqrt() * Decimal(252).sqrt()
    assert abs(inputs.annualized_volatility["AAA"] - closed_form) < Decimal("1e-9")


def test_two_proportional_series_correlate_at_one():
    closes = _alternating("100", "110", 61)
    inputs = compute_market_risk_inputs(
        {"AAA": _bars("AAA", closes), "BBB": _bars("BBB", closes)},
        min_observations=60,
    )
    rho = inputs.correlation_between("AAA", "BBB")
    assert rho is not None
    # Clamped to [-1, 1]; identical return series so this is +1 to Decimal
    # rounding drift.
    assert Decimal(-1) <= rho <= Decimal(1)
    assert abs(rho - Decimal(1)) < Decimal("1e-20")


def test_two_anti_proportional_series_correlate_at_minus_one():
    up = _alternating("100", "110", 61)
    # Same two prices, opposite phase => every daily log return is negated.
    down = _alternating("110", "100", 61)
    inputs = compute_market_risk_inputs(
        {"AAA": _bars("AAA", up), "CCC": _bars("CCC", down)},
        min_observations=60,
    )
    rho = inputs.correlation_between("AAA", "CCC")
    assert rho is not None
    assert Decimal(-1) <= rho <= Decimal(1)
    assert abs(rho - Decimal(-1)) < Decimal("1e-20")


def test_symbol_with_too_few_returns_is_absent_from_volatility():
    # 60 bars => 59 daily returns, one short of the floor.
    closes = _alternating("100", "110", 60)
    inputs = compute_market_risk_inputs(
        {"AAA": _bars("AAA", closes)}, min_observations=60
    )
    assert "AAA" not in inputs.annualized_volatility
    assert inputs.correlation == {}


def test_pair_without_enough_overlap_is_absent_even_when_both_symbols_are_covered():
    # AAA on days 0..70, BBB on days 100..175 - each individually has far
    # more than 60 returns, but they share no dates at all.
    a = _bars("AAA", _alternating("100", "110", 71), first_offset_days=0)
    b = _bars("BBB", _alternating("200", "214", 76), first_offset_days=100)
    inputs = compute_market_risk_inputs({"AAA": a, "BBB": b}, min_observations=60)

    assert inputs.covered("AAA")
    assert inputs.covered("BBB")
    assert inputs.correlation == {}
    assert inputs.correlation_between("AAA", "BBB") is None


def test_constant_price_series_has_zero_variance_and_is_omitted():
    inputs = compute_market_risk_inputs(
        {"FLAT": _bars("FLAT", ["100"] * 80)}, min_observations=60
    )
    assert "FLAT" not in inputs.annualized_volatility


def test_non_positive_close_drops_that_return_and_the_adjacent_one():
    closes = _alternating("100", "105", 65)  # 64 daily returns when clean
    clean = _bars("P", closes)
    assert len(_log_returns_by_date(clean)) == 64

    poisoned = _bars("P", closes)
    poisoned[30] = Bar.model_construct(
        symbol="P",
        bar_interval="1d",
        ts=poisoned[30].ts,
        open=None,
        high=None,
        low=None,
        close=Decimal("0"),
        volume=1,
        source="test-fixture",
    )
    # The bad bar's own return and the next bar's return are both dropped -
    # exactly two, no more - so 64 -> 62.
    assert len(_log_returns_by_date(poisoned)) == 62

    covered = compute_market_risk_inputs({"P": poisoned}, min_observations=62)
    omitted = compute_market_risk_inputs({"P": poisoned}, min_observations=63)
    assert "P" in covered.annualized_volatility
    assert "P" not in omitted.annualized_volatility


# --------------------------------------------------------------------------
# load_market_risk_inputs - real Postgres
# --------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def db_session():
    gen = get_session()
    session = await anext(gen)
    try:
        yield session
    finally:
        await gen.aclose()


@contextlib.asynccontextmanager
async def clean_bars(session, symbols: list[str]):
    try:
        yield
    finally:
        await session.execute(
            delete(MarketDataBar).where(MarketDataBar.symbol.in_(symbols))
        )
        await session.commit()


_LOAD_A = "PRISK-A.US"
_LOAD_B = "PRISK-B.US"
_LOAD_EMPTY = "PRISK-NONE.US"


@pytest.mark.asyncio
async def test_load_market_risk_inputs_matches_the_pure_function_on_the_same_bars():
    a_bars = _bars(_LOAD_A, _alternating("100", "110", 90))
    b_bars = _bars(_LOAD_B, _alternating("50", "56", 90))
    as_of = a_bars[-1].ts.date()

    async with db_session() as session, clean_bars(session, [_LOAD_A, _LOAD_B]):
        store = MarketDataStore(session)
        await store.upsert_bars([*a_bars, *b_bars])
        await session.commit()

        loaded = await load_market_risk_inputs(
            store,
            [_LOAD_A, _LOAD_B, _LOAD_A],  # dupe is de-duped by the loader
            as_of=as_of,
            lookback_days=365,
            min_observations=60,
        )

    expected = compute_market_risk_inputs(
        {_LOAD_A: a_bars, _LOAD_B: b_bars},
        lookback_days=365,
        min_observations=60,
    )
    assert loaded.annualized_volatility == expected.annualized_volatility
    assert loaded.correlation == expected.correlation
    assert loaded.lookback_days == 365
    assert loaded.min_observations == 60


@pytest.mark.asyncio
async def test_load_market_risk_inputs_empty_symbols_is_empty_but_valid():
    async with db_session() as session:
        store = MarketDataStore(session)
        loaded = await load_market_risk_inputs(
            store, [], as_of=date(2026, 6, 30), min_observations=60
        )
    assert loaded.annualized_volatility == {}
    assert loaded.correlation == {}
    assert loaded.lookback_days == 365


@pytest.mark.asyncio
async def test_load_market_risk_inputs_symbol_with_no_rows_is_absent_never_raises():
    async with db_session() as session:
        store = MarketDataStore(session)
        loaded = await load_market_risk_inputs(
            store, [_LOAD_EMPTY], as_of=date(2026, 6, 30), min_observations=60
        )
    assert not loaded.covered(_LOAD_EMPTY)
    assert loaded.annualized_volatility == {}
