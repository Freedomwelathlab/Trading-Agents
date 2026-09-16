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
from decimal import Decimal

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
