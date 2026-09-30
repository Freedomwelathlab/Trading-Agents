"""Transaction-cost modelling for the v2 backtest engine (Phase 70, D088).

**What this closes.** Until this phase, `engine_v2._replay_window` filled
every simulated order at the bar's close, exactly, with no fee, no spread
and no slippage - a grep for `fee`, `commission` or `slippage` across the
whole backtesting package returned nothing. Every backtest figure this
platform had ever produced therefore described a frictionless market that
does not exist. That is a mild distortion for a strategy that trades twice
a year and a decisive one for a strategy that trades several times a day,
where costs are routinely the entire edge; and it distorts in the
FLATTERING direction, which is the dangerous one.

**Costs are applied to the execution PRICE, not deducted from cash.** A
buy fills above the bar's close and a sell below it, and the two
components are folded into one price adjustment. That is not an
approximation - it is an identity. A fee charged as `bps` of notional is
`quantity * price * bps / 10_000`, and adjusting the price by
`price * bps / 10_000` moves the notional by exactly the same amount. So
the single adjusted price gives the account precisely the cash outcome a
separate price-slip plus a separate fee deduction would, while keeping all
the arithmetic inside the existing `PaperBrokerAdapter` fill path rather
than requiring a new way to remove money from a broker.

It also makes position sizing come out right, which a cash deduction would
not: an `all_in` entry sized against the raw close would propose a
quantity the account cannot actually pay for once costs land, and the
paper broker would reject the whole order with `InsufficientFundsError`.
Sizing against the price you will really pay is both more realistic and
the thing that keeps that failure from happening.

**Fees and slippage are still reported separately.** They are
interchangeable in the arithmetic above but not in what an operator can do
about them: a fee is a venue's published schedule, negotiable or reducible
by trading less often, while slippage is a property of liquidity and order
size. `costs_for()` returns the two amounts independently so
`backtest_runs.total_fees` and `total_slippage` can say which one is
eating a strategy.

**This is a simplification, and says so.** A flat bps figure models none
of what really drives execution quality: slippage scales with order size
against available depth, widens in fast markets, and differs between a
resting limit order and a market sweep. Funding costs on a perpetual, and
borrow costs on a short, are not modelled at all - this engine is long-only
and cash-settled, so neither has anything to attach to. What this module
buys is that a backtest is now PESSIMISTIC BY DEFAULT rather than silently
perfect. It does not make a backtest accurate.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

_BPS = Decimal(10_000)
"""Basis points per unit. One place, so no call site divides by a literal
10000 and no call site can get the scale wrong."""


@dataclass(frozen=True)
class CostModel:
    """The cost assumptions one backtest is executed under.

    Frozen, and constructed once per run: the two rates must not change
    partway through a replay, or the resulting equity curve would describe
    no single set of assumptions at all. `engine_v2` copies both onto the
    `BacktestRun` row so a stored result stays interpretable after the
    settings that produced it are edited.
    """

    fee_bps: Decimal
    slippage_bps: Decimal

    def __post_init__(self) -> None:
        if self.fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError(
                "fee_bps and slippage_bps must not be negative - a negative cost is a "
                f"rebate this engine does not model (got fee_bps={self.fee_bps}, "
                f"slippage_bps={self.slippage_bps})."
            )

    @classmethod
    def frictionless(cls) -> "CostModel":
        """Zero fee, zero slippage - the engine's pre-Phase-70 behaviour.

        Exists so a test can assert that this module changes nothing when
        both rates are zero, which is what pins every existing
        engine-behaviour test as still describing the same engine. It is
        NOT a default anywhere in production code: the route builds a
        CostModel from real settings whose defaults are deliberately
        non-zero, precisely so the most optimistic assumption is never the
        one nobody had to choose.
        """
        return cls(fee_bps=Decimal(0), slippage_bps=Decimal(0))

    @property
    def total_bps(self) -> Decimal:
        """The single adverse adjustment applied to an execution price."""
        return self.fee_bps + self.slippage_bps

    def buy_price(self, mid: Decimal) -> Decimal:
        """What a buy actually fills at: above the bar's close, always."""
        return mid * (Decimal(1) + self.total_bps / _BPS)

    def sell_price(self, mid: Decimal) -> Decimal:
        """What a sell actually fills at: below the bar's close, always.

        Floored at zero rather than allowed to go negative, for the
        degenerate case of a cost assumption above 10,000 bps. Nothing
        realistic reaches it; a negative fill price would be nonsense
        propagating into equity, so it is refused here rather than
        discovered later.
        """
        price = mid * (Decimal(1) - self.total_bps / _BPS)
        return price if price > 0 else Decimal(0)

    def costs_for(self, *, quantity: Decimal, mid: Decimal) -> tuple[Decimal, Decimal]:
        """`(fee, slippage)` in currency for one fill of `quantity` at `mid`.

        Both are computed off the UNADJUSTED close, which is what makes the
        two components add up to exactly the difference between the mid
        notional and the executed notional - see the identity in this
        module's docstring. Charging the fee off the already-slipped price
        instead would double-count a sliver of the slippage into the fee.
        """
        notional = quantity * mid
        return (
            notional * self.fee_bps / _BPS,
            notional * self.slippage_bps / _BPS,
        )


# ---------------------------------------------------------------------------
# Spot FX (Phase 103, D123)
# ---------------------------------------------------------------------------

class FillPricer(Protocol):
    """What the bracket simulator needs from a cost model: the price a buy
    and a sell actually fill at, given a mid. `CostModel` (bps, equities)
    and `FxSpreadCostModel` (pips, FX) both satisfy it."""

    def buy_price(self, mid: Decimal) -> Decimal: ...

    def sell_price(self, mid: Decimal) -> Decimal: ...


_DAYS_PER_YEAR = Decimal(365)
_NY = ZoneInfo("America/New_York")


def rollover_days(entry_ts: datetime, exit_ts: datetime) -> int:
    """Financing days charged for a position held from `entry_ts` to `exit_ts`.

    FX positions are rolled at 17:00 New York each weekday. A position open
    across a roll pays (or earns) one day of financing - except Wednesday's
    roll, which carries THREE, because spot settles T+2 and Wednesday's is
    the roll whose value date spans the weekend. Five rolls a week therefore
    charge seven days: Mon 1, Tue 1, Wed 3, Thu 1, Fri 1. There is no roll
    at the Sunday 17:00 open.

    A roll counts when `entry_ts < roll <= exit_ts`: a position opened on
    the roll instant itself has not yet been held across it.
    """
    if exit_ts <= entry_ts:
        return 0
    day = entry_ts.astimezone(_NY).date()
    days = 0
    while True:
        roll = datetime(day.year, day.month, day.day, 17, 0, tzinfo=_NY)
        if roll > exit_ts:
            break
        if roll > entry_ts and roll.weekday() < 5:
            days += 3 if roll.weekday() == 2 else 1
        day = day + timedelta(days=1)
    return days


@dataclass(frozen=True)
class FxSpreadCostModel:
    """Spread-based costs for spot FX / FX CFDs.

    **Why not `CostModel`.** `CostModel` charges basis points of notional,
    which is how an equity venue's commission and a thin book's slippage
    scale. A retail FX CFD venue charges neither: its whole fee is the
    SPREAD it quotes, a fixed number of pips regardless of price level or
    (at retail size) order size. Expressing 0.5 pips on EUR/USD as bps
    would be a coincidence of today's price, and wrong for USD/JPY by two
    orders of magnitude.

    **The fill rule.** Bars are MID prices (see `marketdata/fx.py`). A buy
    fills at mid + half-spread and a sell at mid - half-spread, so a round
    trip costs one full spread. There is no volume-based slippage because
    there is no volume; a stop that gaps is already filled at the bar's
    open by the bracket simulator rather than at the stop.

    **Financing** is optional and off by default (`None`). When set it is
    an ANNUAL rate on notional, per direction, charged per rollover day
    (`rollover_days`). A venue's real overnight charge is its admin fee
    plus or minus the tom-next interest differential, which moves with both
    currencies' policy rates; this model takes the rate as an input rather
    than inventing a curve. An intraday run that is flat by the 17:00 NY
    roll never crosses one and pays none.

    Duck-type compatible with `CostModel` where the intraday engine and the
    bracket simulator touch it: `buy_price`, `sell_price`, `costs_for`.
    """

    pair: str
    pip_size: Decimal
    half_spread_pips: Decimal
    financing_annual_rate_long: Decimal | None = None
    financing_annual_rate_short: Decimal | None = None

    def __post_init__(self) -> None:
        if self.pip_size <= 0:
            raise ValueError(f"pip_size must be positive; got {self.pip_size}.")
        if self.half_spread_pips < 0:
            raise ValueError(
                f"half_spread_pips must not be negative; got {self.half_spread_pips}."
            )

    @classmethod
    def for_symbol(
        cls,
        symbol: str,
        *,
        half_spread_pips: Decimal | None = None,
        financing_annual_rate_long: Decimal | None = None,
        financing_annual_rate_short: Decimal | None = None,
    ) -> "FxSpreadCostModel":
        """Per-pair defaults from `marketdata.fx.FX_PAIRS`, any overridden."""
        from apps.api.app.marketdata.fx import pair_spec

        spec = pair_spec(symbol)
        return cls(
            pair=spec.pair,
            pip_size=spec.pip_size,
            half_spread_pips=(
                spec.default_half_spread_pips if half_spread_pips is None else half_spread_pips
            ),
            financing_annual_rate_long=financing_annual_rate_long,
            financing_annual_rate_short=financing_annual_rate_short,
        )

    @property
    def half_spread(self) -> Decimal:
        """Half the spread in PRICE units."""
        return self.half_spread_pips * self.pip_size

    def buy_price(self, mid: Decimal) -> Decimal:
        return mid + self.half_spread

    def sell_price(self, mid: Decimal) -> Decimal:
        price = mid - self.half_spread
        return price if price > 0 else Decimal(0)

    def costs_for(self, *, quantity: Decimal, mid: Decimal) -> tuple[Decimal, Decimal]:
        """`(fee, spread)` for one fill: no commission, half a spread."""
        return (Decimal(0), quantity * self.half_spread)

    def financing_for(
        self,
        *,
        is_long: bool,
        quantity: Decimal,
        price: Decimal,
        entry_ts: datetime,
        exit_ts: datetime,
    ) -> Decimal:
        """Financing cost (positive = paid) for holding `quantity` from
        `entry_ts` to `exit_ts`. Zero when no rate is configured."""
        rate = self.financing_annual_rate_long if is_long else self.financing_annual_rate_short
        if rate is None:
            return Decimal(0)
        days = rollover_days(entry_ts, exit_ts)
        if days == 0:
            return Decimal(0)
        return quantity * price * rate * Decimal(days) / _DAYS_PER_YEAR
