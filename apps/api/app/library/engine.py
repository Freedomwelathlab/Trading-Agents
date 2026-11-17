"""Daily, weights-based portfolio backtest for the Strategy Library.

A strategy is a pure function of prices up to and including one close that
returns target portfolio weights. The engine does the rest, the same way
for every strategy:

  * **No look-ahead.** A decision made on close `t` is traded on close
    `t + lag` (default one session later) and first earns on `t + lag + 1`.
  * **Costs on every change.** One-way cost in basis points, by market,
    charged on the absolute weight traded.
  * **Cash earns nothing.** Conservative: the published versions of these
    rules usually credit T-bill interest to the idle sleeve.
  * **Adjusted prices.** Dividends and splits are in the return series.

Statistics are floats - they are research measurements, not money. Every
order this platform sends still sizes and prices in `Decimal`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from apps.api.app.marketdata.providers.yahoo import DailyHistory

MIN_TRADE_WEIGHT = 0.005
"""A weight change under half a percent is not traded: it would be a real
commission spent correcting drift nobody would bother to correct."""

Weights = dict[str, float]
StrategyFn = Callable[["PriceTable", int, dict[str, Any], dict[str, Any]], Weights | None]


class PriceTable:
    """Adjusted closes for several symbols on one shared calendar.

    The calendar is the union of every symbol's trading days. On a day one
    market is shut its symbol is carried at its last close, which is what a
    holder's statement would show - a mark, not an invented bar. Before a
    symbol's first real bar its price is `None` and no strategy may hold it.
    """

    def __init__(self, histories: Sequence[DailyHistory]) -> None:
        if not histories:
            raise ValueError("PriceTable needs at least one history.")
        self.symbols: tuple[str, ...] = tuple(h.symbol for h in histories)
        self.dates: tuple[date, ...] = tuple(sorted({d for h in histories for d in h.dates}))
        index = {d: i for i, d in enumerate(self.dates)}
        n = len(self.dates)
        self.px: dict[str, list[float | None]] = {}
        self.real: dict[str, list[int]] = {}
        self.last_close: dict[str, float] = {}
        self.currency: dict[str, str | None] = {}
        for h in histories:
            row: list[float | None] = [None] * n
            for d, value in zip(h.dates, h.adj_closes, strict=True):
                row[index[d]] = value
            counts = [0] * n
            seen = 0
            last: float | None = None
            for i in range(n):
                if row[i] is not None:
                    seen += 1
                    last = row[i]
                else:
                    row[i] = last
                counts[i] = seen
            self.px[h.symbol] = row
            self.real[h.symbol] = counts
            self.last_close[h.symbol] = h.closes[-1]
            self.currency[h.symbol] = h.currency
        self._sma: dict[tuple[str, int], list[float | None]] = {}
        self._rsi: dict[tuple[str, int], list[float | None]] = {}

    def __len__(self) -> int:
        return len(self.dates)

    def price(self, symbol: str, i: int) -> float | None:
        return self.px[symbol][i] if i >= 0 else None

    def bars(self, symbol: str, i: int) -> int:
        """How many REAL bars this symbol has up to and including `i`."""
        return self.real[symbol][i] if i >= 0 else 0

    def ret(self, symbol: str, i: int, lookback: int) -> float | None:
        if i - lookback < 0:
            return None
        then, now = self.px[symbol][i - lookback], self.px[symbol][i]
        if then is None or now is None or then <= 0:
            return None
        return now / then - 1.0

    def sma(self, symbol: str, i: int, period: int) -> float | None:
        key = (symbol, period)
        series = self._sma.get(key)
        if series is None:
            series = _rolling_mean(self.px[symbol], period)
            self._sma[key] = series
        return series[i]

    def rsi(self, symbol: str, i: int, period: int) -> float | None:
        key = (symbol, period)
        series = self._rsi.get(key)
        if series is None:
            series = _wilder_rsi(self.px[symbol], period)
            self._rsi[key] = series
        return series[i]

    def highest(self, symbol: str, i: int, period: int) -> float | None:
        """Highest close of the `period` sessions BEFORE `i`."""
        window = self.px[symbol][max(0, i - period) : i]
        if len(window) < period or any(v is None for v in window):
            return None
        return max(v for v in window if v is not None)

    def lowest(self, symbol: str, i: int, period: int) -> float | None:
        window = self.px[symbol][max(0, i - period) : i]
        if len(window) < period or any(v is None for v in window):
            return None
        return min(v for v in window if v is not None)

    def realised_vol(self, symbol: str, i: int, period: int) -> float | None:
        """Annualised standard deviation of the last `period` daily returns."""
        if i - period < 0:
            return None
        row = self.px[symbol]
        rets: list[float] = []
        for k in range(i - period + 1, i + 1):
            a, b = row[k - 1], row[k]
            if a is None or b is None or a <= 0:
                return None
            rets.append(b / a - 1.0)
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var) * math.sqrt(self.periods_per_year())

    def is_month_end(self, i: int) -> bool:
        """The last session of a calendar month.

        For every row but the last that is simply "the next row is in
        another month". The last row has no next row, so the calendar
        answers instead: it is a month end when the next day this table
        would trade (the next weekday, or the next day for a seven-day
        market) falls in another month. That lets a live bot see a
        month-end decision on the session it is made, as the backtest
        does, rather than a session late. A holiday on the true last
        weekday is the one case it misses, by one session.
        """
        if i + 1 < len(self.dates):
            return self.dates[i + 1].month != self.dates[i].month
        last = self.dates[i]
        nxt = last + timedelta(days=1)
        if not self._trades_weekends():
            while nxt.weekday() >= 5:
                nxt += timedelta(days=1)
        return nxt.month != last.month

    def _trades_weekends(self) -> bool:
        return any(d.weekday() >= 5 for d in self.dates[-10:])

    def periods_per_year(self) -> float:
        span = (self.dates[-1] - self.dates[0]).days
        if span <= 0:
            return 252.0
        return (len(self.dates) - 1) / (span / 365.25)


def _rolling_mean(row: Sequence[float | None], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(row)
    total = 0.0
    valid = 0
    for i, value in enumerate(row):
        if value is not None:
            total += value
            valid += 1
        if i >= period:
            old = row[i - period]
            if old is not None:
                total -= old
                valid -= 1
        if valid == period:
            out[i] = total / period
    return out


def _wilder_rsi(row: Sequence[float | None], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(row)
    avg_gain = avg_loss = 0.0
    count = 0
    prev: float | None = None
    for i, value in enumerate(row):
        if value is None:
            continue
        if prev is not None:
            change = value - prev
            gain, loss = max(change, 0.0), max(-change, 0.0)
            count += 1
            if count <= period:
                avg_gain += gain / period
                avg_loss += loss / period
            else:
                avg_gain = (avg_gain * (period - 1) + gain) / period
                avg_loss = (avg_loss * (period - 1) + loss) / period
            if count >= period:
                if avg_loss == 0:
                    out[i] = 100.0 if avg_gain > 0 else 50.0
                else:
                    out[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
        prev = value
    return out


@dataclass
class RunResult:
    dates: tuple[date, ...]
    """Evaluation dates only - the warm-up is excluded."""
    curve: list[float]
    """Equity, rebased to 1.0 on the first evaluation date."""
    exposure: list[float]
    """Gross exposure (sum of absolute weights) held INTO each date."""
    trades: int
    turnover: float
    """Total one-way weight traded over the evaluation."""
    cost_paid: float
    """Sum of the fractions of equity spent on costs."""
    target: Weights
    """The most recent target the strategy decided."""
    decided_on: date | None
    holdings: Weights = field(default_factory=dict)
    """Weights actually held after the final date's trading."""
    explain: dict[str, str] = field(default_factory=dict)


def _clean(weights: Weights) -> Weights:
    return {s: float(w) for s, w in weights.items() if abs(w) > 1e-9}


def run_strategy(
    table: PriceTable,
    fn: StrategyFn,
    params: dict[str, Any],
    *,
    eval_index: int,
    lag: int = 1,
    cost_bps: Callable[[str], float],
) -> RunResult:
    """Run `fn` over the whole table; measure from `eval_index` onward.

    The strategy sees every row, including the warm-up, so its state is
    already live when measurement starts. Returns and costs before
    `eval_index` are not counted.
    """
    n = len(table)
    if not 0 <= eval_index < n:
        raise ValueError("eval_index outside the table.")
    if lag < 0:
        raise ValueError("lag must be zero or more sessions.")

    held: Weights = {}
    equity = 1.0
    curve: list[float] = []
    exposure: list[float] = []
    pending: list[tuple[int, Weights]] = []
    state: dict[str, Any] = {}
    target: Weights = {}
    decided_on: date | None = None
    trades = 0
    turnover = 0.0
    cost_paid = 0.0

    for i in range(n):
        gross_in = sum(abs(w) for w in held.values())
        if i > 0 and held:
            day = 0.0
            moved: Weights = {}
            for symbol, weight in held.items():
                before, after = table.px[symbol][i - 1], table.px[symbol][i]
                r = (after / before - 1.0) if before and after else 0.0
                day += weight * r
                moved[symbol] = weight * (1.0 + r)
            growth = 1.0 + day
            if i > eval_index:
                equity *= max(growth, 0.0)
            held = _clean({s: w / growth for s, w in moved.items()}) if growth > 0 else {}

        decision = fn(table, i, params, state)
        if decision is not None:
            target = _clean(decision)
            decided_on = table.dates[i]
            pending.append((i + lag, target))

        # Every decision whose session has arrived, oldest first; when more
        # than one is due the latest is the one that stands.
        due = [wanted for when, wanted in pending if when <= i]
        pending = [(when, wanted) for when, wanted in pending if when > i]
        if due:
            wanted = due[-1]
            new: Weights = {}
            traded = 0.0
            cost = 0.0
            for symbol in set(held) | set(wanted):
                have, want = held.get(symbol, 0.0), wanted.get(symbol, 0.0)
                if table.px[symbol][i] is None:
                    want = 0.0  # cannot buy what has not listed yet
                delta = want - have
                if abs(delta) >= MIN_TRADE_WEIGHT or (want == 0.0 and have != 0.0):
                    traded += abs(delta)
                    cost += abs(delta) * cost_bps(symbol) / 10_000.0
                    new[symbol] = want
                else:
                    new[symbol] = have
            if traded > 0:
                held = _clean(new)
                if i >= eval_index:
                    equity *= max(1.0 - cost, 0.0)
                    trades += 1
                    turnover += traded
                    cost_paid += cost

        if i >= eval_index:
            curve.append(equity)
            exposure.append(gross_in if i > eval_index else 0.0)

    return RunResult(
        dates=table.dates[eval_index:],
        curve=curve,
        exposure=exposure,
        trades=trades,
        turnover=turnover,
        cost_paid=cost_paid,
        target=target,
        decided_on=decided_on,
        holdings=held,
        explain=dict(state.get("explain", {})),
    )
