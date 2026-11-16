"""Performance statistics for a daily equity curve.

Every figure is `None` when there is not enough data to compute it - a
ratio is never reported as zero because its denominator was. Risk-free
rate is taken as zero throughout, so Sharpe and Sortino here are slightly
generous to anything that sat in the market while rates were high; the
same convention is applied to the benchmark, so comparisons hold.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

MIN_DAYS = 60
"""Fewer sessions than this and annualised statistics are noise."""


@dataclass(frozen=True)
class Metrics:
    start: date
    end: date
    days: int
    years: float
    total_return: float
    cagr: float | None
    volatility: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float
    calmar: float | None
    t_stat: float | None
    """Mean daily return over its standard error. Above ~2 the mean is
    unlikely to be zero by chance; it says nothing about the future."""
    best_year: float | None
    worst_year: float | None
    positive_years: int
    total_years: int

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["start"] = self.start.isoformat()
        out["end"] = self.end.isoformat()
        return out


def daily_returns(curve: Sequence[float]) -> list[float]:
    return [curve[i] / curve[i - 1] - 1.0 for i in range(1, len(curve)) if curve[i - 1] > 0]


def max_drawdown(curve: Sequence[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def yearly_returns(dates: Sequence[date], curve: Sequence[float]) -> list[tuple[int, float]]:
    """Calendar-year returns. The first and last year may be partial."""
    out: list[tuple[int, float]] = []
    if not dates:
        return out
    start_value = curve[0]
    year = dates[0].year
    last = curve[0]
    for d, value in zip(dates, curve, strict=True):
        if d.year != year:
            if start_value > 0:
                out.append((year, last / start_value - 1.0))
            year = d.year
            start_value = last
        last = value
    if start_value > 0:
        out.append((year, last / start_value - 1.0))
    return out


def compute_metrics(dates: Sequence[date], curve: Sequence[float]) -> Metrics | None:
    """Statistics for one stretch of an equity curve, or `None` when the
    stretch is too short to say anything."""
    if len(dates) != len(curve) or len(curve) < 2 or curve[0] <= 0:
        return None
    rets = daily_returns(curve)
    days = len(rets)
    span_days = (dates[-1] - dates[0]).days
    years = span_days / 365.25 if span_days > 0 else 0.0
    total = curve[-1] / curve[0] - 1.0
    enough = days >= MIN_DAYS and years > 0
    per_year = days / years if years > 0 else 252.0

    cagr = volatility = sharpe = sortino = calmar = t_stat = None
    if enough:
        if curve[-1] > 0:
            cagr = (curve[-1] / curve[0]) ** (1.0 / years) - 1.0
        mean = sum(rets) / days
        var = sum((r - mean) ** 2 for r in rets) / (days - 1)
        std = math.sqrt(var)
        if std > 0:
            volatility = std * math.sqrt(per_year)
            sharpe = mean / std * math.sqrt(per_year)
            t_stat = mean / (std / math.sqrt(days))
        downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / days)
        if downside > 0:
            sortino = mean / downside * math.sqrt(per_year)
    drawdown = max_drawdown(curve)
    if cagr is not None and drawdown < 0:
        calmar = cagr / abs(drawdown)

    by_year = [r for _, r in yearly_returns(dates, curve)]
    return Metrics(
        start=dates[0],
        end=dates[-1],
        days=days,
        years=round(years, 2),
        total_return=total,
        cagr=cagr,
        volatility=volatility,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=drawdown,
        calmar=calmar,
        t_stat=t_stat,
        best_year=max(by_year) if by_year else None,
        worst_year=min(by_year) if by_year else None,
        positive_years=sum(1 for r in by_year if r > 0),
        total_years=len(by_year),
    )


def segment(
    dates: Sequence[date], curve: Sequence[float], *, start: date | None, end: date | None
) -> Metrics | None:
    """Metrics for the part of the curve inside [start, end]."""
    picked = [
        (d, v)
        for d, v in zip(dates, curve, strict=True)
        if (start is None or d >= start) and (end is None or d <= end)
    ]
    if len(picked) < 2:
        return None
    return compute_metrics([d for d, _ in picked], [v for _, v in picked])
