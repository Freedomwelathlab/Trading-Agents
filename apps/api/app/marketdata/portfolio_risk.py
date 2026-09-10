"""Compute the per-symbol annualized volatility and pairwise correlation the
trade-path Portfolio Manager's two Phase-62 constraints need (D079), from the
daily closes already persisted in `market_data_bars` (Phase 53, D070).

Split in two on purpose, the same split `apps/api/app/marketdata/store.py`
and the risk-engine data loaders use:

- `compute_market_risk_inputs(bars_by_symbol, ...)` is PURE - it takes the
  bars the caller already holds and returns a `MarketRiskInputs`. Every
  statistic (log returns, sample standard deviation, Pearson correlation,
  the sqrt(252) annualization) is deterministic Decimal arithmetic, no
  float, matching the no-float rule the rest of this codebase holds for
  anything a risk decision reads.
- `load_market_risk_inputs(store, symbols, ...)` is the thin async wrapper
  that does the `MarketDataStore` reads and calls the pure function. The
  route calls this; `decide()` never sees either - it is handed the
  finished `MarketRiskInputs` as plain data (D029 rejected a market-data
  dependency inside the Portfolio Manager).

**Never fabricates a number.** A symbol with fewer than `min_observations`
overlapping daily returns in the window is simply absent from
`annualized_volatility`; a pair with fewer than `min_observations`
overlapping days is absent from `correlation`. The Portfolio Manager reads
that absence as "skip this check, audited" - it does not fall back to a
zero, a market average, or a prior value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal

from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.store import MarketDataStore
from apps.api.app.portfolio_manager.models import MarketRiskInputs

# Trading days per year - the standard convention for annualizing a daily
# volatility. A constant, not a setting: changing it would silently move
# every volatility figure a limit is compared against.
_TRADING_DAYS_PER_YEAR = 252

DEFAULT_LOOKBACK_DAYS = 365
"""Trailing CALENDAR days of bars requested. ~252 trading days fall inside
it - a year of daily returns, the shortest window that gives a stable
annualized volatility without reaching so far back that a regime change
dominates it."""

DEFAULT_MIN_OBSERVATIONS = 60
"""Minimum overlapping daily returns for a symbol (or a pair) to be
'covered'. ~3 trading months: below this a standard deviation is too noisy
to gate a real trade on, so the check is skipped rather than run on it."""


def _log_returns_by_date(bars: Sequence[Bar]) -> dict[date, Decimal]:
    """Daily log returns keyed by the bar's date. `bars` is oldest-first
    (the `MarketDataStore` contract). A non-positive close is dropped rather
    than fed to `ln` - it cannot be a real equity price."""
    returns: dict[date, Decimal] = {}
    previous: Decimal | None = None
    for bar in bars:
        close = bar.close
        if close is None or close <= 0:
            previous = None
            continue
        if previous is not None:
            returns[bar.ts.date()] = (close / previous).ln()
        previous = close
    return returns


def _std_dev(values: Sequence[Decimal]) -> Decimal | None:
    """Sample standard deviation (n - 1 in the denominator). `None` for
    fewer than two values or a constant series."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values, Decimal(0)) / n
    variance = sum(((v - mean) ** 2 for v in values), Decimal(0)) / (n - 1)
    if variance <= 0:
        return None
    return variance.sqrt()


def _pearson(xs: Sequence[Decimal], ys: Sequence[Decimal]) -> Decimal | None:
    """Pearson correlation of two equal-length series. `None` when either
    series is constant (zero variance) or shorter than two points. Clamped
    to [-1, 1] against Decimal rounding drift."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mean_x = sum(xs, Decimal(0)) / n
    mean_y = sum(ys, Decimal(0)) / n
    cov = sum(((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)), Decimal(0))
    var_x = sum(((x - mean_x) ** 2 for x in xs), Decimal(0))
    var_y = sum(((y - mean_y) ** 2 for y in ys), Decimal(0))
    if var_x <= 0 or var_y <= 0:
        return None
    rho = cov / (var_x.sqrt() * var_y.sqrt())
    return max(Decimal(-1), min(Decimal(1), rho))


def compute_market_risk_inputs(
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
) -> MarketRiskInputs:
    """Annualized volatility per symbol and Pearson correlation per pair,
    from daily log returns. Pure: `bars_by_symbol` is whatever the caller
    already read, oldest-first per symbol.

    A symbol with < `min_observations` daily returns is omitted from
    `annualized_volatility`; a pair with < `min_observations` overlapping
    dates is omitted from `correlation`. Nothing is inferred for a gap.
    """
    returns_by_symbol = {
        symbol: _log_returns_by_date(bars) for symbol, bars in bars_by_symbol.items()
    }

    annualized_volatility: dict[str, Decimal] = {}
    annualization = Decimal(_TRADING_DAYS_PER_YEAR).sqrt()
    for symbol, returns in returns_by_symbol.items():
        if len(returns) < min_observations:
            continue
        daily = _std_dev(list(returns.values()))
        if daily is not None:
            annualized_volatility[symbol] = daily * annualization

    correlation: dict[str, Decimal] = {}
    covered = sorted(annualized_volatility)
    for i, sym_a in enumerate(covered):
        for sym_b in covered[i + 1 :]:
            shared = sorted(
                returns_by_symbol[sym_a].keys() & returns_by_symbol[sym_b].keys()
            )
            if len(shared) < min_observations:
                continue
            rho = _pearson(
                [returns_by_symbol[sym_a][d] for d in shared],
                [returns_by_symbol[sym_b][d] for d in shared],
            )
            if rho is not None:
                correlation[MarketRiskInputs._pair_key(sym_a, sym_b)] = rho

    return MarketRiskInputs(
        annualized_volatility=annualized_volatility,
        correlation=correlation,
        lookback_days=lookback_days,
        min_observations=min_observations,
    )


async def load_market_risk_inputs(
    store: MarketDataStore,
    symbols: Sequence[str],
    *,
    as_of: date,
    bar_interval: str = "1d",
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
) -> MarketRiskInputs:
    """Read the trailing `lookback_days` of bars for each distinct symbol
    and compute the risk inputs. `as_of` is the window's end (the trade
    date); the daily bar for `as_of` itself is included if present.

    Returns an empty-but-valid `MarketRiskInputs` when `symbols` is empty or
    nothing is ingested - the Portfolio Manager then simply skips both
    Phase-62 checks, which is the correct behaviour, not an error.
    """
    start_date = as_of - timedelta(days=lookback_days)
    bars_by_symbol: dict[str, list[Bar]] = {}
    for symbol in dict.fromkeys(symbols):  # de-dupe, order-preserving
        bars_by_symbol[symbol] = await store.get_bars(
            symbol, bar_interval=bar_interval, start_date=start_date, end_date=as_of
        )
    return compute_market_risk_inputs(
        bars_by_symbol, lookback_days=lookback_days, min_observations=min_observations
    )
