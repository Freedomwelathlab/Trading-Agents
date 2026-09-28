"""A MODELLED options backtest over daily underlying bars (Phase 100, D119).

**Read this first: every option price this engine produces is MODELLED.**
It is not historical option data. This platform has never had past option
chains - nothing free publishes them, and Longbridge cannot even quote the
live chain for this account (`301604`). So a past trade here is priced the
only way it can be: Black-Scholes (`pricing.py`) from the underlying's
real daily close, with a volatility that is itself an ASSUMPTION:

    sigma(t) = realised volatility of the last `rv_window` daily log
               returns, annualised  x  an IV/RV multiplier

The multiplier stands in for the implied-over-realised premium that real
option prices carry. It is either derived from stored REAL chain snapshots
(median ATM IV / RV over the snapshot dates - see
`derive_iv_rv_multiplier`) or, when there are none, a flat
`DEFAULT_IV_RV_MULTIPLIER` of 1.2; the result says which. A flat multiplier
has no skew, no term structure and no volatility-of-volatility, and every
conclusion drawn from this engine inherits those omissions. Above all:
**with a multiplier above 1, premium SELLING is profitable by construction
whenever realised volatility is merely stable**, because the model charges
buyers more than the moves it then replays. Read short-premium results with
that in mind.

**Where real prices exist they are used, and labelled.** If a stored
`option_chain_snapshots` day exists for an entry date, the structure is
chosen from that day's real listed expiries and strikes by the VENDOR's
delta, and priced at the real mid with the real half-spread. A mark on a
later day uses the real quote whenever that day's snapshot carries the
contract. Each trade is labelled:

* `OBSERVED` - every price at entry and exit was a stored quote (or exact
  expiry settlement);
* `MODELLED` - no stored quote was used at all;
* `MIXED`   - some legs or one side of the trade were modelled. MIXED is
  NOT observed and must not be reported as such.

**Costs.** A modelled leg is filled at mid +- a half-spread of
`max(min_half_spread, half_spread_pct x mid)` (default max($0.02, 3%)):
buying pays mid + half-spread, selling receives mid - half-spread (floored
at zero). An observed leg uses its real bid/ask instead. Every leg pays
`commission_per_contract` on entry and again when closed; a leg that
expires worthless pays nothing to close. An in-the-money leg at expiry is
settled at intrinsic value from the underlying's close, as if cash-settled
- real TQQQ options are physically settled, which this does not model.

**Mechanics.** One structure (one contract of each leg) at a time. When
flat at a daily close inside the entry window, a new structure is opened
at that close with expiry on the first Friday on or after `dte_days`
calendar days later. It is checked at each subsequent close - profit
target and stop are fractions of the entry premium (debit paid, or credit
received) and are evaluated on the price the position could actually be
closed at, after the half-spread; exits happen at the close, so a gap
through a stop fills at the gapped price, not at the stop. A position still
open at the window's last bar is closed there (`window_end`). Nothing here
looks ahead: volatility at a close uses returns up to and including that
close only.

Pure and synchronous (no I/O) so a walk-forward can run thousands of
configurations; `load_backtest_inputs` is the one async reader.
"""

from __future__ import annotations

import enum
import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.store import MarketDataStore
from apps.api.app.options.pricing import OptionRight, implied_delta_strike, theoretical_price
from apps.api.app.options.strategies import select_strikes
from apps.api.app.options.structures import CONTRACT_MULTIPLIER, StructureKind, build_vertical

_NEW_YORK = ZoneInfo("America/New_York")
_CENT = Decimal("0.01")

MODELLED_NOTE = (
    "MODELLED: option prices are Black-Scholes values from the underlying's real daily closes "
    "and an assumed volatility (realised vol x IV/RV multiplier). They are not historical "
    "option quotes. Trades labelled OBSERVED used stored real chain snapshots at entry and "
    "exit; MIXED trades used them for only part of the trade."
)

DEFAULT_IV_RV_MULTIPLIER = 1.2
"""Used only when no stored snapshot can supply a measured ratio. A common
rough figure for index-like underlyings; an assumption, reported as one."""

TRADING_DAYS_PER_YEAR = 252


class OptionStrategy(str, enum.Enum):  # noqa: UP042 (str mixin matches this codebase)
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    BULL_CALL = "bull_call"      # debit vertical
    BEAR_PUT = "bear_put"        # debit vertical
    BULL_PUT = "bull_put"        # credit vertical
    BEAR_CALL = "bear_call"      # credit vertical
    IRON_CONDOR = "iron_condor"  # bull put + bear call; execution non-native (structures.py)

    @property
    def is_credit(self) -> bool:
        return self in (
            OptionStrategy.BULL_PUT,
            OptionStrategy.BEAR_CALL,
            OptionStrategy.IRON_CONDOR,
        )


class PricingSource(str, enum.Enum):  # noqa: UP042
    MODELLED = "MODELLED"
    OBSERVED = "OBSERVED"
    MIXED = "MIXED"


class ExitReason(str, enum.Enum):  # noqa: UP042
    PROFIT_TARGET = "profit_target"
    STOP_LOSS = "stop_loss"
    DTE_EXIT = "dte_exit"
    EXPIRY = "expiry"
    WINDOW_END = "window_end"


# --- inputs ------------------------------------------------------------------


@dataclass(frozen=True)
class DailyClose:
    """One real daily close. A float because it feeds a float model; it is
    a price input, not an amount of money this engine reports."""

    day: date
    close: float


def closes_from_bars(bars: Sequence[Bar]) -> list[DailyClose]:
    """Stored 1d bars -> closes keyed by their New York session date."""
    return [
        DailyClose(day=b.ts.astimezone(_NEW_YORK).date(), close=float(b.close))
        for b in bars
        if b.close is not None and b.close > 0
    ]


@dataclass(frozen=True)
class ObservedQuote:
    expiry: date
    right: OptionRight
    strike: Decimal
    bid: Decimal | None
    ask: Decimal | None
    iv: Decimal | None = None
    delta: Decimal | None = None

    @property
    def two_sided(self) -> bool:
        """A usable market: both sides present, not crossed, ask above zero.
        A one-sided quote has no mid, and a mid invented from one side is a
        price nobody offered."""
        return (
            self.bid is not None
            and self.ask is not None
            and self.ask > 0
            and self.ask >= self.bid
        )

    @property
    def mid(self) -> Decimal | None:
        if not self.two_sided:
            return None
        assert self.bid is not None and self.ask is not None
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class ObservedChain:
    """One stored snapshot day, as the backtest reads it."""

    trade_date: date
    spot: Decimal | None
    quotes: tuple[ObservedQuote, ...]
    _index: dict[tuple[date, OptionRight, Decimal], ObservedQuote] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        for q in self.quotes:
            self._index[(q.expiry, q.right, q.strike.normalize())] = q

    def find(self, expiry: date, right: OptionRight, strike: Decimal) -> ObservedQuote | None:
        return self._index.get((expiry, right, strike.normalize()))

    def expiries_after(self, day: date) -> list[date]:
        return sorted({q.expiry for q in self.quotes if q.expiry > day})


def observed_chains_from_rows(rows: Sequence[Any]) -> dict[date, ObservedChain]:
    """`OptionChainSnapshot` rows -> one `ObservedChain` per trade date."""
    by_day: dict[date, list[Any]] = {}
    for r in rows:
        by_day.setdefault(r.trade_date, []).append(r)
    out: dict[date, ObservedChain] = {}
    for day, day_rows in by_day.items():
        out[day] = ObservedChain(
            trade_date=day,
            spot=next((r.underlying_price for r in day_rows if r.underlying_price), None),
            quotes=tuple(
                ObservedQuote(
                    expiry=r.expiry,
                    right=OptionRight(r.right),
                    strike=Decimal(r.strike),
                    bid=r.bid,
                    ask=r.ask,
                    iv=r.iv,
                    delta=r.delta,
                )
                for r in day_rows
            ),
        )
    return out


# --- configuration -------------------------------------------------------------


@dataclass(frozen=True)
class OptionsBacktestConfig:
    """One point in the research grid.

    `target_delta` is a MAGNITUDE (0-1) and means: the long leg for long
    options and debit spreads; the SOLD leg for credit spreads and each side
    of an iron condor. `wing_delta` is the other leg; omitted, it is
    `target - 0.25` (floor 0.05) for a debit spread and `target / 2` for a
    credit spread - which reproduces the playbook's credit pair (0.30/0.15)
    exactly and sits beside its debit pair (0.60/0.30 -> 0.60/0.35).

    `profit_target_pct` / `stop_loss_pct` are fractions of the entry
    premium: for a debit, 0.5 = +50% of what was paid; for a credit, 0.5 =
    half the credit captured, and a stop of 1.0 = a loss equal to the
    credit. None disables that exit. `exit_dte` > 0 closes when that many
    calendar days (or fewer) remain; 0 holds to expiry.
    """

    strategy: OptionStrategy
    target_delta: float = 0.30
    wing_delta: float | None = None
    dte_days: int = 14
    profit_target_pct: float | None = 0.5
    stop_loss_pct: float | None = 1.0
    exit_dte: int = 0
    rv_window: int = 20
    risk_free_rate: float = 0.04
    strike_increment: Decimal = Decimal("1")
    min_half_spread: Decimal = Decimal("0.02")
    half_spread_pct: Decimal = Decimal("0.03")
    commission_per_contract: Decimal = Decimal("0.65")

    def __post_init__(self) -> None:
        if not 0.0 < self.target_delta < 1.0:
            raise ValueError("target_delta must be strictly between 0 and 1.")
        if self.wing_delta is not None and not 0.0 < self.wing_delta < 1.0:
            raise ValueError("wing_delta must be strictly between 0 and 1.")
        if self.dte_days < 1:
            raise ValueError("dte_days must be at least 1: a daily-bar backtest cannot hold 0DTE.")
        if self.rv_window < 2:
            raise ValueError("rv_window must be at least 2.")
        if self.exit_dte < 0:
            raise ValueError("exit_dte must be non-negative.")

    @property
    def resolved_wing_delta(self) -> float:
        if self.wing_delta is not None:
            return self.wing_delta
        if self.strategy.is_credit:
            return self.target_delta / 2
        return max(self.target_delta - 0.25, 0.05)

    @property
    def label(self) -> str:
        pt = "none" if self.profit_target_pct is None else f"{self.profit_target_pct:.0%}"
        sl = "none" if self.stop_loss_pct is None else f"{self.stop_loss_pct:.0%}"
        return (
            f"{self.strategy.value} d{self.target_delta:.2f} {self.dte_days}DTE "
            f"pt={pt} sl={sl}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "target_delta": self.target_delta,
            "wing_delta": self.resolved_wing_delta,
            "dte_days": self.dte_days,
            "profit_target_pct": self.profit_target_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "exit_dte": self.exit_dte,
            "rv_window": self.rv_window,
            "risk_free_rate": self.risk_free_rate,
            "strike_increment": str(self.strike_increment),
            "min_half_spread": str(self.min_half_spread),
            "half_spread_pct": str(self.half_spread_pct),
            "commission_per_contract": str(self.commission_per_contract),
        }


# --- results -------------------------------------------------------------------


@dataclass(frozen=True)
class TradeLeg:
    right: OptionRight
    strike: Decimal
    is_long: bool
    entry_price: Decimal
    """Reference price per share at entry (mid): MODELLED unless the trade
    says otherwise."""
    exit_price: Decimal
    """Reference price per share at exit (mid, or intrinsic at expiry)."""


@dataclass(frozen=True)
class OptionsTrade:
    entry_date: date
    exit_date: date
    expiry: date
    strategy: OptionStrategy
    legs: tuple[TradeLeg, ...]
    entry_spot: Decimal
    exit_spot: Decimal
    entry_premium: Decimal
    """Per share, after half-spreads. Positive = debit paid; negative =
    credit received."""
    exit_value: Decimal
    """Per share, after half-spreads: what closing the structure returned
    (negative when closing cost money)."""
    commissions: Decimal
    pnl: Decimal
    """Dollars for one structure (x100), net of half-spreads and commissions."""
    max_loss: Decimal
    """Dollars for one structure: the defined risk at entry, plus entry and
    exit commissions."""
    return_on_risk: float
    exit_reason: ExitReason
    volatility_at_entry: float
    pricing: PricingSource

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_date": self.entry_date.isoformat(),
            "exit_date": self.exit_date.isoformat(),
            "expiry": self.expiry.isoformat(),
            "strategy": self.strategy.value,
            "legs": [
                {
                    "right": leg.right.value,
                    "strike": str(leg.strike),
                    "side": "long" if leg.is_long else "short",
                    "entry_price": str(leg.entry_price),
                    "exit_price": str(leg.exit_price),
                }
                for leg in self.legs
            ],
            "entry_spot": str(self.entry_spot),
            "exit_spot": str(self.exit_spot),
            "entry_premium": str(self.entry_premium),
            "exit_value": str(self.exit_value),
            "commissions": str(self.commissions),
            "pnl": str(self.pnl),
            "max_loss": str(self.max_loss),
            "return_on_risk": round(self.return_on_risk, 4),
            "exit_reason": self.exit_reason.value,
            "volatility_at_entry": round(self.volatility_at_entry, 4),
            "pricing": self.pricing.value,
        }


@dataclass(frozen=True)
class TradeStats:
    trades: int
    wins: int
    win_rate: float | None
    total_pnl: Decimal
    expectancy: Decimal | None
    """Mean dollar P&L per trade (one structure)."""
    expectancy_r: float | None
    """Mean return on risk per trade (P&L / max loss)."""
    t_stat_r: float | None
    max_drawdown: Decimal
    """Largest peak-to-trough fall of cumulative closed-trade P&L, dollars."""
    max_drawdown_r: float
    pricing_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": None if self.win_rate is None else round(self.win_rate, 4),
            "total_pnl": str(self.total_pnl),
            "expectancy": None if self.expectancy is None else str(self.expectancy),
            "expectancy_r": None if self.expectancy_r is None else round(self.expectancy_r, 4),
            "t_stat_r": None if self.t_stat_r is None else round(self.t_stat_r, 3),
            "max_drawdown": str(self.max_drawdown),
            "max_drawdown_r": round(self.max_drawdown_r, 4),
            "pricing_counts": dict(self.pricing_counts),
        }


def trade_stats(trades: Sequence[OptionsTrade]) -> TradeStats:
    """Metrics over trades in the order they were closed. Empty input gives
    None for every ratio - never a zero that reads as a measured result."""
    n = len(trades)
    pricing = Counter(t.pricing.value for t in trades)
    if n == 0:
        return TradeStats(0, 0, None, Decimal(0), None, None, None, Decimal(0), 0.0, {})
    wins = sum(1 for t in trades if t.pnl > 0)
    total = sum((t.pnl for t in trades), Decimal(0))
    rs = [t.return_on_risk for t in trades]
    t_stat: float | None = None
    if n >= 2:
        sd = statistics.stdev(rs)
        if sd > 0:
            t_stat = statistics.fmean(rs) / (sd / math.sqrt(n))
    peak = trough_dd = Decimal(0)
    cum = Decimal(0)
    peak_r = dd_r = cum_r = 0.0
    for t in trades:
        cum += t.pnl
        peak = max(peak, cum)
        trough_dd = max(trough_dd, peak - cum)
        cum_r += t.return_on_risk
        peak_r = max(peak_r, cum_r)
        dd_r = max(dd_r, peak_r - cum_r)
    return TradeStats(
        trades=n,
        wins=wins,
        win_rate=wins / n,
        total_pnl=total,
        expectancy=(total / n).quantize(_CENT, rounding=ROUND_HALF_UP),
        expectancy_r=statistics.fmean(rs),
        t_stat_r=t_stat,
        max_drawdown=trough_dd,
        max_drawdown_r=dd_r,
        pricing_counts=dict(pricing),
    )


@dataclass(frozen=True)
class OptionsBacktestResult:
    config: OptionsBacktestConfig
    iv_rv_multiplier: float
    window_start: date | None
    window_end: date | None
    trades: tuple[OptionsTrade, ...]
    refused_entries: dict[str, int]
    stats: TradeStats
    pricing_basis: str = "MODELLED"
    note: str = MODELLED_NOTE

    def to_dict(self, *, include_trades: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pricing_basis": self.pricing_basis,
            "note": self.note,
            "config": self.config.to_dict(),
            "label": self.config.label,
            "iv_rv_multiplier": self.iv_rv_multiplier,
            "window_start": None if self.window_start is None else self.window_start.isoformat(),
            "window_end": None if self.window_end is None else self.window_end.isoformat(),
            "refused_entries": dict(self.refused_entries),
            "stats": self.stats.to_dict(),
        }
        if include_trades:
            out["trades"] = [t.to_dict() for t in self.trades]
        return out


# --- the series ----------------------------------------------------------------


@dataclass(frozen=True)
class PreparedSeries:
    """Closes plus trailing realised volatility, computed once per
    `rv_window` so a grid search does not recompute it per configuration.
    `rv[i]` uses log returns up to and including close i - never later."""

    days: tuple[date, ...]
    closes: tuple[float, ...]
    rv: tuple[float | None, ...]
    rv_window: int

    def index_on_or_after(self, day: date) -> int:
        for i, d in enumerate(self.days):
            if d >= day:
                return i
        return len(self.days)

    def index_on_or_before(self, day: date) -> int:
        for i in range(len(self.days) - 1, -1, -1):
            if self.days[i] <= day:
                return i
        return -1


def prepare_series(closes: Sequence[DailyClose], rv_window: int) -> PreparedSeries:
    ordered = sorted(closes, key=lambda c: c.day)
    days = tuple(c.day for c in ordered)
    px = tuple(c.close for c in ordered)
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))]
    rv: list[float | None] = [None] * len(px)
    for i in range(rv_window, len(px)):
        window = rets[i - rv_window : i]  # returns ending at close i
        rv[i] = statistics.stdev(window) * math.sqrt(TRADING_DAYS_PER_YEAR)
    return PreparedSeries(days=days, closes=px, rv=tuple(rv), rv_window=rv_window)


# --- structure construction ----------------------------------------------------


@dataclass(frozen=True)
class _LegSpec:
    right: OptionRight
    strike: Decimal
    is_long: bool


_VERTICAL_KIND = {
    OptionStrategy.BULL_CALL: StructureKind.BULL_CALL,
    OptionStrategy.BEAR_PUT: StructureKind.BEAR_PUT,
    OptionStrategy.BULL_PUT: StructureKind.BULL_PUT,
    OptionStrategy.BEAR_CALL: StructureKind.BEAR_CALL,
}


def modelled_expiry(entry_day: date, dte_days: int) -> date:
    """The first Friday on or after `entry_day + dte_days` - weekly
    expiries are Fridays; TQQQ also lists Mon/Wed weeklies, which a daily
    model does not need to pick between."""
    target = entry_day + timedelta(days=dte_days)
    return target + timedelta(days=(4 - target.weekday()) % 7)


def _snap(raw: float, increment: Decimal) -> Decimal:
    return (Decimal(str(raw)) / increment).quantize(Decimal(1), rounding=ROUND_HALF_UP) * increment


def _vertical_legs(
    strategy: OptionStrategy,
    config: OptionsBacktestConfig,
    *,
    spot: float,
    years: float,
    sigma: float,
) -> tuple[_LegSpec, _LegSpec]:
    """Strikes by delta through the Phase 78 selector, then the Phase 75
    constructor's defined-risk guard (raises ValueError when the model
    cannot produce a genuinely bounded structure)."""
    kind = _VERTICAL_KIND[strategy]
    if strategy.is_credit:
        long_d, short_d = config.resolved_wing_delta, config.target_delta
    else:
        long_d, short_d = config.target_delta, config.resolved_wing_delta
    long_k, short_k = select_strikes(
        kind=kind,
        spot=spot,
        dte_years=years,
        volatility=sigma,
        strike_increment=config.strike_increment,
        long_delta=long_d,
        short_delta=short_d,
        risk_free_rate=config.risk_free_rate,
    )
    spread = build_vertical(
        kind=kind,
        spot=spot,
        long_strike=long_k,
        short_strike=short_k,
        dte_years=years,
        volatility=sigma,
        risk_free_rate=config.risk_free_rate,
    )
    right = spread.long_leg.right
    return _LegSpec(right, long_k, True), _LegSpec(right, short_k, False)


def _modelled_legs(
    config: OptionsBacktestConfig, *, spot: float, years: float, sigma: float
) -> tuple[_LegSpec, ...]:
    s = config.strategy
    if s in (OptionStrategy.LONG_CALL, OptionStrategy.LONG_PUT):
        right = OptionRight.CALL if s is OptionStrategy.LONG_CALL else OptionRight.PUT
        k = _snap(
            implied_delta_strike(
                spot=spot,
                target_delta=config.target_delta,
                time_to_expiry_years=years,
                volatility=sigma,
                right=right,
                risk_free_rate=config.risk_free_rate,
            ),
            config.strike_increment,
        )
        if k <= 0:
            raise ValueError("strike snapped to zero")
        return (_LegSpec(right, k, True),)
    if s is OptionStrategy.IRON_CONDOR:
        puts = _vertical_legs(OptionStrategy.BULL_PUT, config, spot=spot, years=years, sigma=sigma)
        calls = _vertical_legs(
            OptionStrategy.BEAR_CALL, config, spot=spot, years=years, sigma=sigma
        )
        short_put = next(leg for leg in puts if not leg.is_long)
        short_call = next(leg for leg in calls if not leg.is_long)
        if short_put.strike >= short_call.strike:
            raise ValueError("iron condor short strikes overlap")
        return puts + calls
    return _vertical_legs(s, config, spot=spot, years=years, sigma=sigma)


def _leg_plan(config: OptionsBacktestConfig) -> list[tuple[OptionRight, float, bool]]:
    """(right, |delta| wanted, is_long) per leg, for choosing REAL contracts."""
    t, w = config.target_delta, config.resolved_wing_delta
    C, P = OptionRight.CALL, OptionRight.PUT
    s = config.strategy
    plans = {
        OptionStrategy.LONG_CALL: [(C, t, True)],
        OptionStrategy.LONG_PUT: [(P, t, True)],
        OptionStrategy.BULL_CALL: [(C, t, True), (C, w, False)],
        OptionStrategy.BEAR_PUT: [(P, t, True), (P, w, False)],
        OptionStrategy.BULL_PUT: [(P, w, True), (P, t, False)],
        OptionStrategy.BEAR_CALL: [(C, w, True), (C, t, False)],
        OptionStrategy.IRON_CONDOR: [(P, w, True), (P, t, False), (C, w, True), (C, t, False)],
    }
    return plans[s]


def _legs_are_valid(strategy: OptionStrategy, legs: Sequence[_LegSpec]) -> bool:
    """The strike ordering each structure needs to be what its name says
    (and defined-risk). Applied to REAL contracts chosen by vendor delta,
    which can collapse onto one strike on a sparse chain."""
    def strikes(right: OptionRight, is_long: bool) -> Decimal:
        return next(leg.strike for leg in legs if leg.right is right and leg.is_long is is_long)

    C, P = OptionRight.CALL, OptionRight.PUT
    if strategy in (OptionStrategy.LONG_CALL, OptionStrategy.LONG_PUT):
        return True
    if strategy is OptionStrategy.BULL_CALL:
        return strikes(C, True) < strikes(C, False)
    if strategy is OptionStrategy.BEAR_PUT:
        return strikes(P, True) > strikes(P, False)
    if strategy is OptionStrategy.BULL_PUT:
        return strikes(P, True) < strikes(P, False)
    if strategy is OptionStrategy.BEAR_CALL:
        return strikes(C, True) > strikes(C, False)
    return (
        strikes(P, True) < strikes(P, False) < strikes(C, False) < strikes(C, True)
    )


def _observed_structure(
    config: OptionsBacktestConfig, chain: ObservedChain, day: date
) -> tuple[date, tuple[_LegSpec, ...]] | None:
    """Real contracts from a stored snapshot: the listed expiry nearest the
    target DTE (later on a tie), each leg at the strike whose VENDOR delta
    is nearest the wanted magnitude among two-sided quotes. None when the
    snapshot cannot supply a valid structure - the caller then models it,
    and the trade is labelled accordingly."""
    expiries = chain.expiries_after(day)
    if not expiries:
        return None
    expiry = min(expiries, key=lambda e: (abs((e - day).days - config.dte_days), -e.toordinal()))
    legs: list[_LegSpec] = []
    for right, want, is_long in _leg_plan(config):
        candidates = [
            q
            for q in chain.quotes
            if q.expiry == expiry and q.right is right and q.two_sided and q.delta is not None
        ]
        if not candidates:
            return None
        best = min(candidates, key=lambda q: (abs(abs(float(q.delta or 0)) - want), q.strike))
        legs.append(_LegSpec(right, best.strike, is_long))
    if not _legs_are_valid(config.strategy, legs):
        return None
    return expiry, tuple(legs)


# --- the engine ----------------------------------------------------------------


@dataclass
class _Position:
    entry_index: int
    entry_day: date
    expiry: date
    legs: tuple[_LegSpec, ...]
    entry_mids: tuple[float, ...]
    entry_premium: float
    basis: float
    max_loss: Decimal
    entry_sigma: float
    observed_entry: bool


def _intrinsic(right: OptionRight, strike: float, spot: float) -> float:
    return max(spot - strike, 0.0) if right is OptionRight.CALL else max(strike - spot, 0.0)


def _half_spread(mid: float, config: OptionsBacktestConfig) -> float:
    return max(float(config.min_half_spread), float(config.half_spread_pct) * mid)


def _money(x: float) -> Decimal:
    return Decimal(str(x)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _quote_legs(
    legs: Sequence[_LegSpec],
    *,
    expiry: date,
    day: date,
    spot: float,
    sigma: float,
    config: OptionsBacktestConfig,
    chain: ObservedChain | None,
) -> tuple[list[float], list[float], bool, bool]:
    """(mids, half_spreads, any_observed, any_modelled) for every leg."""
    years = max((expiry - day).days, 0) / 365.0
    mids: list[float] = []
    halves: list[float] = []
    any_obs = any_mod = False
    for leg in legs:
        q = chain.find(expiry, leg.right, leg.strike) if chain is not None else None
        mid = q.mid if q is not None else None
        if q is not None and mid is not None and q.bid is not None and q.ask is not None:
            mids.append(float(mid))
            halves.append(float(q.ask - q.bid) / 2)
            any_obs = True
            continue
        p = theoretical_price(
            spot=spot,
            strike=float(leg.strike),
            time_to_expiry_years=years,
            volatility=sigma,
            right=leg.right,
            risk_free_rate=config.risk_free_rate,
        )
        mids.append(p)
        halves.append(_half_spread(p, config))
        any_mod = True
    return mids, halves, any_obs, any_mod


def _open_value(legs: Sequence[_LegSpec], mids: Sequence[float], halves: Sequence[float]) -> float:
    """Per share: pay the offer on longs, receive the bid on shorts."""
    total = 0.0
    for leg, m, h in zip(legs, mids, halves, strict=True):
        total += (m + h) if leg.is_long else -max(m - h, 0.0)
    return total


def _close_value(legs: Sequence[_LegSpec], mids: Sequence[float], halves: Sequence[float]) -> float:
    """Per share: receive the bid on longs, pay the offer on shorts."""
    total = 0.0
    for leg, m, h in zip(legs, mids, halves, strict=True):
        total += max(m - h, 0.0) if leg.is_long else -(m + h)
    return total


def _structural_max_loss(
    strategy: OptionStrategy, legs: Sequence[_LegSpec], premium: float
) -> float:
    """Per share, from the ENTRY premium actually paid or received."""
    if not strategy.is_credit:
        return premium  # a debit structure can lose at most what was paid
    credit = -premium
    widths: list[float] = []
    for right in (OptionRight.PUT, OptionRight.CALL):
        ks = [float(leg.strike) for leg in legs if leg.right is right]
        if len(ks) == 2:
            widths.append(abs(ks[0] - ks[1]))
    return max(widths) - credit


def run_options_backtest(
    series: PreparedSeries | Sequence[DailyClose],
    config: OptionsBacktestConfig,
    *,
    iv_rv_multiplier: float,
    start: date | None = None,
    end: date | None = None,
    observed: Mapping[date, ObservedChain] | None = None,
) -> OptionsBacktestResult:
    """Replay daily closes and trade `config` one structure at a time.

    Entries are allowed on closes in [start, end) - never on the window's
    last bar - and anything open at the last bar on or before `end` is
    closed there. Bars before `start` serve only as volatility warm-up.
    Every price is MODELLED unless a stored snapshot supplied it; see the
    module docstring for exactly what that means.
    """
    if iv_rv_multiplier <= 0:
        raise ValueError("iv_rv_multiplier must be positive.")
    if not isinstance(series, PreparedSeries) or series.rv_window != config.rv_window:
        closes = (
            [DailyClose(d, c) for d, c in zip(series.days, series.closes, strict=True)]
            if isinstance(series, PreparedSeries)
            else list(series)
        )
        series = prepare_series(closes, config.rv_window)
    observed = observed or {}

    days, px, rv = series.days, series.closes, series.rv
    # Bars before the first volatility estimate are warm-up only: nothing
    # can be priced there, so they are not iterated (and not counted as
    # refused entries).
    first = max(
        series.index_on_or_after(start) if start is not None else 0, series.rv_window
    )
    last = series.index_on_or_before(end) if end is not None else len(days) - 1
    trades: list[OptionsTrade] = []
    refused: Counter[str] = Counter()
    pos: _Position | None = None
    commission = float(config.commission_per_contract)

    def close_position(
        exit_day: date, spot: float, reason: ExitReason, *, settle: bool,
        mids: Sequence[float] = (), halves: Sequence[float] = (),
        exit_obs: bool = False, exit_mod: bool = False,
    ) -> None:
        nonlocal pos
        assert pos is not None
        if settle:
            exit_mids = [_intrinsic(leg.right, float(leg.strike), spot) for leg in pos.legs]
            value = sum(
                m if leg.is_long else -m for leg, m in zip(pos.legs, exit_mids, strict=True)
            )
            closing_legs = sum(1 for m in exit_mids if m > 0)
        else:
            exit_mids = list(mids)
            value = _close_value(pos.legs, mids, halves)
            closing_legs = len(pos.legs)
        comm = commission * (len(pos.legs) + closing_legs)
        pnl = (value - pos.entry_premium) * CONTRACT_MULTIPLIER - comm
        any_obs = pos.observed_entry or exit_obs
        any_mod = (not pos.observed_entry) or exit_mod
        pricing = (
            PricingSource.MODELLED
            if not any_obs
            else (PricingSource.MIXED if any_mod else PricingSource.OBSERVED)
        )
        pnl_d = _money(pnl)
        trades.append(
            OptionsTrade(
                entry_date=pos.entry_day,
                exit_date=exit_day,
                expiry=pos.expiry,
                strategy=config.strategy,
                legs=tuple(
                    TradeLeg(
                        right=leg.right,
                        strike=leg.strike,
                        is_long=leg.is_long,
                        entry_price=_money(em),
                        exit_price=_money(xm),
                    )
                    for leg, em, xm in zip(pos.legs, pos.entry_mids, exit_mids, strict=True)
                ),
                entry_spot=_money(px[pos.entry_index]),
                exit_spot=_money(spot),
                entry_premium=_money(pos.entry_premium),
                exit_value=_money(value),
                commissions=_money(comm),
                pnl=pnl_d,
                max_loss=pos.max_loss,
                return_on_risk=float(pnl_d / pos.max_loss),
                exit_reason=reason,
                volatility_at_entry=pos.entry_sigma,
                pricing=pricing,
            )
        )
        pos = None

    for i in range(first, last + 1):
        day, spot = days[i], px[i]
        chain = observed.get(day)
        sigma_raw = rv[i]
        sigma = sigma_raw * iv_rv_multiplier if sigma_raw is not None else None

        if pos is not None:
            if pos.expiry < day:
                # Expired between two bars (a holiday Friday): settle on the
                # last close on or before expiry, which is the previous bar.
                close_position(pos.expiry, px[i - 1], ExitReason.EXPIRY, settle=True)
            elif pos.expiry == day:
                close_position(day, spot, ExitReason.EXPIRY, settle=True)
            else:
                mark_sigma = sigma if sigma is not None else pos.entry_sigma
                mids, halves, obs, mod = _quote_legs(
                    pos.legs, expiry=pos.expiry, day=day, spot=spot,
                    sigma=mark_sigma, config=config, chain=chain,
                )
                pnl_share = _close_value(pos.legs, mids, halves) - pos.entry_premium
                reason: ExitReason | None = None
                if (
                    config.profit_target_pct is not None
                    and pnl_share >= config.profit_target_pct * pos.basis
                ):
                    reason = ExitReason.PROFIT_TARGET
                elif (
                    config.stop_loss_pct is not None
                    and pnl_share <= -config.stop_loss_pct * pos.basis
                ):
                    reason = ExitReason.STOP_LOSS
                elif config.exit_dte > 0 and (pos.expiry - day).days <= config.exit_dte:
                    reason = ExitReason.DTE_EXIT
                elif i == last:
                    reason = ExitReason.WINDOW_END
                if reason is not None:
                    close_position(
                        day, spot, reason, settle=False,
                        mids=mids, halves=halves, exit_obs=obs, exit_mod=mod,
                    )

        if pos is not None or i >= last:
            continue
        if sigma is None or sigma <= 0:
            refused["no_volatility_history"] += 1
            continue

        legs: tuple[_LegSpec, ...] | None = None
        expiry: date | None = None
        if chain is not None:
            picked = _observed_structure(config, chain, day)
            if picked is not None:
                expiry, legs = picked
        if legs is None:
            expiry = modelled_expiry(day, config.dte_days)
            try:
                legs = _modelled_legs(
                    config, spot=spot, years=(expiry - day).days / 365.0, sigma=sigma
                )
            except ValueError:
                refused["not_defined_risk"] += 1
                continue
        assert expiry is not None
        mids, halves, obs, mod = _quote_legs(
            legs, expiry=expiry, day=day, spot=spot, sigma=sigma, config=config, chain=chain,
        )
        premium = _open_value(legs, mids, halves)
        basis = abs(premium)
        if (config.strategy.is_credit and premium >= 0) or (
            not config.strategy.is_credit and premium <= 0
        ):
            refused["no_premium_after_costs"] += 1
            continue
        structural = _structural_max_loss(config.strategy, legs, premium)
        if structural <= 0:
            refused["not_defined_risk"] += 1
            continue
        max_loss = _money(structural * CONTRACT_MULTIPLIER + commission * len(legs) * 2)
        pos = _Position(
            entry_index=i,
            entry_day=day,
            expiry=expiry,
            legs=legs,
            entry_mids=tuple(mids),
            entry_premium=premium,
            basis=basis,
            max_loss=max_loss,
            entry_sigma=sigma,
            # Part real, part modelled is not an observed entry: the trade
            # can then be MIXED at best.
            observed_entry=obs and not mod,
        )

    stats = trade_stats(trades)
    touched_real = stats.pricing_counts.get("OBSERVED", 0) + stats.pricing_counts.get("MIXED", 0)
    basis_label = "MODELLED" if touched_real == 0 else "MODELLED+OBSERVED"
    return OptionsBacktestResult(
        config=config,
        iv_rv_multiplier=iv_rv_multiplier,
        window_start=days[first] if first < len(days) else None,
        window_end=days[last] if 0 <= last < len(days) else None,
        trades=tuple(trades),
        refused_entries=dict(refused),
        stats=stats,
        pricing_basis=basis_label,
    )


# --- the IV/RV multiplier --------------------------------------------------------


@dataclass(frozen=True)
class IvRvMultiplier:
    value: float
    source: str
    samples: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"value": round(self.value, 4), "source": self.source, "samples": list(self.samples)}


def atm_implied_vol(chain: ObservedChain, *, target_dte: int = 30) -> tuple[float, date] | None:
    """The vendor's ATM IV: the expiry nearest `target_dte`, the strike
    nearest spot, the mean of that strike's call and put IV (whichever the
    vendor printed). None when the snapshot lacks a spot or any IV there."""
    if chain.spot is None:
        return None
    spot = chain.spot
    expiries = chain.expiries_after(chain.trade_date)
    if not expiries:
        return None
    expiry = min(expiries, key=lambda e: abs((e - chain.trade_date).days - target_dte))
    at_expiry = [q for q in chain.quotes if q.expiry == expiry and q.iv is not None and q.iv > 0]
    if not at_expiry:
        return None
    strike = min({q.strike for q in at_expiry}, key=lambda k: abs(k - spot))
    ivs = [float(q.iv) for q in at_expiry if q.strike == strike and q.iv is not None]
    return statistics.fmean(ivs), expiry


def derive_iv_rv_multiplier(
    series: PreparedSeries,
    observed: Mapping[date, ObservedChain],
    *,
    target_dte: int = 30,
) -> IvRvMultiplier:
    """Median over snapshot days of (vendor ATM IV / realised vol that day).

    A snapshot day only counts when the stored daily bars cover it (so the
    realised vol is the one the backtest itself would use); days that do
    not are skipped and listed as skipped, never filled. With no usable day
    the default is returned and the source says so."""
    samples: list[dict[str, Any]] = []
    ratios: list[float] = []
    index = {d: i for i, d in enumerate(series.days)}
    for day in sorted(observed):
        chain = observed[day]
        atm = atm_implied_vol(chain, target_dte=target_dte)
        i = index.get(day)
        rv = series.rv[i] if i is not None else None
        if atm is None or rv is None or rv <= 0:
            samples.append(
                {
                    "trade_date": day.isoformat(),
                    "used": False,
                    "why": "no ATM IV in snapshot" if atm is None else "no daily bar/RV that day",
                }
            )
            continue
        iv, expiry = atm
        ratios.append(iv / rv)
        samples.append(
            {
                "trade_date": day.isoformat(),
                "used": True,
                "atm_iv": round(iv, 4),
                "expiry": expiry.isoformat(),
                f"rv{series.rv_window}": round(rv, 4),
                "ratio": round(iv / rv, 4),
            }
        )
    if not ratios:
        return IvRvMultiplier(
            DEFAULT_IV_RV_MULTIPLIER,
            f"default {DEFAULT_IV_RV_MULTIPLIER} (no stored snapshot usable to measure ATM IV / "
            f"RV{series.rv_window})",
            tuple(samples),
        )
    return IvRvMultiplier(
        statistics.median(ratios),
        f"measured: median ATM IV / RV{series.rv_window} over {len(ratios)} stored snapshot "
        f"day(s)",
        tuple(samples),
    )


# --- the one async reader ----------------------------------------------------------


async def load_backtest_inputs(
    session: AsyncSession, underlying: str, *, start: date, end: date
) -> tuple[list[DailyClose], dict[date, ObservedChain]]:
    """Real stored daily bars and any stored snapshots for the window."""
    from apps.api.app.options.snapshots import load_snapshots_between

    bars = await MarketDataStore(session).get_bars(
        underlying, bar_interval="1d", start_date=start, end_date=end
    )
    rows = await load_snapshots_between(session, underlying, start, end)
    return closes_from_bars(bars), observed_chains_from_rows(rows)
