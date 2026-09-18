"""Option strategy selection for the US market (Phase 78, D096).

The decision layer of the TQQQ options playbook, sitting on the Phase 75
foundation (pricing / structures / selection). It answers one question:
**given an underlying signal, what defined-risk option structure — if any —
should be traded, at what strikes, in what size?**

Four of the playbook's rules are enforced here rather than left to a
caller, because each is a rule the playbook states as mandatory:

  * **§5 scoring, 0-12.** Evidence is scored with the playbook's own
    weights. The thresholds it proposes (A+ at 10-12, candidate at 8-9,
    watchlist at 6-7) are carried as its numbers and labelled as its
    numbers: the playbook itself says the score "must be validated rather
    than assumed to predict profitability."
  * **§16 hard no-trade: score < 8/12 refuses.** Not a warning, a refusal.
  * **§10A dual path.** A directional edge routes to a DEBIT vertical
    (reversal path); a range with rich IV routes to a defined-risk CREDIT
    vertical (premium path). Neither path ever produces a naked short.
  * **§3 sizing tiers.** A+ 0.50%, normal 0.25-0.40%, 0DTE <= 0.20% of
    account equity, sized off the structure's exact max loss.

Every refusal is a typed object carrying a reason, never a silent `None`,
because "why did it not trade" is the question an operator actually asks.

**Everything priced here is modeled** (see `pricing.py`): there is no
historical option chain, so a backtest of these plans prices legs from the
underlying. Live use must re-price against real quotes before submitting.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from apps.api.app.options.pricing import OptionRight, implied_delta_strike
from apps.api.app.options.selection import (
    DteBand,
    classify_dte,
    iv_regime_appropriate,
)
from apps.api.app.options.structures import (
    StructureKind,
    VerticalSpread,
    build_vertical,
    contracts_for_risk,
)


class StrategyPath(str, enum.Enum):  # noqa: UP042 (str mixin matches this codebase)
    """§10A: the regime engine branches into two independent paths."""

    REVERSAL = "reversal"   # directional edge -> debit vertical
    PREMIUM = "premium"     # range + rich IV -> defined-risk credit vertical
    NO_TRADE = "no_trade"


class SignalGrade(str, enum.Enum):  # noqa: UP042
    A_PLUS = "a_plus"          # 10-12
    CANDIDATE = "candidate"    # 8-9
    WATCHLIST = "watchlist"    # 6-7
    NO_TRADE = "no_trade"      # <6


@dataclass(frozen=True)
class SignalEvidence:
    """The §5 evidence table, as booleans.

    Kept as explicit named flags rather than a bare integer so a recorded
    plan shows WHICH evidence produced its score — a 9 built from a sweep
    plus a structure shift is a different trade from a 9 built from four
    soft confirmations.
    """

    liquidity_sweep: bool = False          # +2
    market_structure_shift: bool = False   # +2
    cross_market_confirm: bool = False     # +1  (QQQ/NDX)
    vwap_location: bool = False            # +1
    rsi_divergence: bool = False           # +1
    volume_confirm: bool = False           # +1
    atr_confirm: bool = False              # +1
    major_level: bool = False              # +1
    option_liquidity_ok: bool = False      # +1
    iv_appropriate: bool = False           # +1


_WEIGHTS: list[tuple[str, int]] = [
    ("liquidity_sweep", 2),
    ("market_structure_shift", 2),
    ("cross_market_confirm", 1),
    ("vwap_location", 1),
    ("rsi_divergence", 1),
    ("volume_confirm", 1),
    ("atr_confirm", 1),
    ("major_level", 1),
    ("option_liquidity_ok", 1),
    ("iv_appropriate", 1),
]

MAX_SCORE = sum(w for _, w in _WEIGHTS)  # 12


def score_signal(evidence: SignalEvidence) -> int:
    """§5's 0-12 score, using the playbook's own weights."""
    return sum(w for name, w in _WEIGHTS if getattr(evidence, name))


def grade_score(score: int) -> SignalGrade:
    if score >= 10:
        return SignalGrade.A_PLUS
    if score >= 8:
        return SignalGrade.CANDIDATE
    if score >= 6:
        return SignalGrade.WATCHLIST
    return SignalGrade.NO_TRADE


MINIMUM_TRADEABLE_SCORE = 8
"""§16: "the bot must not trade when signal score < 8/12." A hard refusal.
Watchlist grade (6-7) is explicitly NOT tradeable."""


# --- §3 account risk tiers -------------------------------------------------

RISK_A_PLUS = Decimal("0.005")       # 0.50%
RISK_NORMAL = Decimal("0.0035")      # mid of the 0.25-0.40% band
RISK_SPECULATIVE = Decimal("0.002")  # <= 0.20% for 0DTE


def risk_budget_pct(grade: SignalGrade, dte_band: DteBand) -> Decimal:
    """§3 tiers. 0DTE is capped at the speculative tier REGARDLESS of grade
    — the playbook gives 0DTE its own ceiling because gamma/theta make it a
    different risk, not a better trade."""
    if dte_band is DteBand.ZERO_DTE:
        return RISK_SPECULATIVE
    if grade is SignalGrade.A_PLUS:
        return RISK_A_PLUS
    return RISK_NORMAL


# --- §10A regime router ----------------------------------------------------


def route_path(
    *,
    has_directional_edge: bool,
    is_range_bound: bool,
    iv_rank: float,
    directional_score: int,
) -> StrategyPath:
    """§10A. A strong directional signal suppresses premium selling near the
    same level ("do not open opposing positions that create hidden portfolio
    exposure"); a flat tape with rich IV routes to premium; anything else is
    no trade.

    Directional edge wins ties deliberately: the playbook keeps the reversal
    engine as the primary path and adds premium only when there is NOT a
    high-confidence directional read.
    """
    if has_directional_edge and directional_score >= MINIMUM_TRADEABLE_SCORE:
        return StrategyPath.REVERSAL
    if is_range_bound and iv_rank >= 0.5:
        return StrategyPath.PREMIUM
    return StrategyPath.NO_TRADE


# --- strike selection ------------------------------------------------------

DEBIT_LONG_DELTA = 0.60    # §2 band 0.50-0.65
DEBIT_SHORT_DELTA = 0.30   # §2 band 0.20-0.35
CREDIT_SHORT_DELTA = 0.30  # sold leg, near the money
CREDIT_LONG_DELTA = 0.15   # protective wing, further out


def _round_strike(raw: float, increment: Decimal) -> Decimal:
    inc = increment
    return (Decimal(str(raw)) / inc).quantize(Decimal(1), rounding=ROUND_HALF_UP) * inc


def select_strikes(
    *,
    kind: StructureKind,
    spot: float,
    dte_years: float,
    volatility: float,
    strike_increment: Decimal = Decimal("1"),
) -> tuple[Decimal, Decimal]:
    """(long_strike, short_strike) for a vertical, chosen by DELTA and then
    snapped to the chain's strike grid.

    Delta-first is the playbook's rule — a chain is quoted by strike, so the
    band has to be inverted. After snapping, if rounding collapses the two
    legs onto one strike the short leg is pushed one increment further out,
    because a zero-width "spread" is not a structure and would divide by
    zero in the risk math.
    """
    if kind is StructureKind.BULL_CALL:
        right, long_d, short_d = OptionRight.CALL, DEBIT_LONG_DELTA, DEBIT_SHORT_DELTA
    elif kind is StructureKind.BEAR_PUT:
        right, long_d, short_d = OptionRight.PUT, DEBIT_LONG_DELTA, DEBIT_SHORT_DELTA
    elif kind is StructureKind.BULL_PUT:
        right, long_d, short_d = OptionRight.PUT, CREDIT_LONG_DELTA, CREDIT_SHORT_DELTA
    elif kind is StructureKind.BEAR_CALL:
        right, long_d, short_d = OptionRight.CALL, CREDIT_LONG_DELTA, CREDIT_SHORT_DELTA
    else:
        raise ValueError(f"{kind} is not a vertical.")

    def strike_for(delta: float) -> Decimal:
        return _round_strike(
            implied_delta_strike(
                spot=spot,
                target_delta=delta,
                time_to_expiry_years=dte_years,
                volatility=volatility,
                right=right,
            ),
            strike_increment,
        )

    long_strike, short_strike = strike_for(long_d), strike_for(short_d)

    # Enforce the ordering each structure requires, widening rather than
    # silently building an invalid (or zero-width) spread.
    wants_long_below = kind in (StructureKind.BULL_CALL, StructureKind.BULL_PUT)
    if wants_long_below and long_strike >= short_strike:
        short_strike = long_strike + strike_increment
    elif not wants_long_below and long_strike <= short_strike:
        short_strike = long_strike - strike_increment

    return long_strike, short_strike


ZERO_DTE_DEFAULT_HOURS = 4.0
"""A literal zero time-to-expiry has NO time value, which makes delta a step
function and collapses a modeled spread to max_loss == 0 — a number that
would divide the sizing math into nonsense. The playbook's "0DTE" means
*expiring today*, with hours of the session still to run, so a 0DTE plan is
modeled with the hours actually remaining. This default is a placeholder for
a live clock, not a claim about any particular moment."""


def _time_to_expiry_years(dte_days: int, zero_dte_hours_remaining: float) -> float:
    if dte_days > 0:
        return dte_days / 365.0
    if zero_dte_hours_remaining <= 0:
        raise ValueError(
            "A 0DTE plan needs the hours still remaining in the session; "
            "zero time to expiry cannot be modeled."
        )
    return zero_dte_hours_remaining / 24.0 / 365.0


# --- the plan --------------------------------------------------------------


@dataclass(frozen=True)
class OptionTradePlan:
    path: StrategyPath
    spread: VerticalSpread
    contracts: int
    score: int
    grade: SignalGrade
    risk_budget: Decimal
    dte_band: DteBand
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def max_loss_total(self) -> Decimal:
        return self.spread.max_loss * self.contracts

    @property
    def max_profit_total(self) -> Decimal:
        return self.spread.max_profit * self.contracts


@dataclass(frozen=True)
class TradeRefusal:
    """Why no trade was placed. A typed reason, never a bare None — "why did
    it not trade" is the question an operator actually asks."""

    reason: str
    score: int | None = None
    grade: SignalGrade | None = None


def plan_option_trade(
    *,
    bullish: bool,
    evidence: SignalEvidence,
    spot: float,
    dte_days: int,
    volatility: float,
    iv_rank: float,
    account_equity: Decimal,
    has_directional_edge: bool = True,
    is_range_bound: bool = False,
    strike_increment: Decimal = Decimal("1"),
    zero_dte_hours_remaining: float = ZERO_DTE_DEFAULT_HOURS,
) -> OptionTradePlan | TradeRefusal:
    """Turn an underlying signal into a defined-risk option plan, or refuse.

    The order of checks matters and follows the playbook: score the
    evidence, apply the hard score floor, route the regime, only then pick
    a structure and size it. Sizing last means the risk tier is chosen from
    a grade that already passed every gate.
    """
    score = score_signal(evidence)
    grade = grade_score(score)
    dte_band = classify_dte(dte_days)

    if dte_band is DteBand.TOO_LONG:
        return TradeRefusal(
            f"DTE {dte_days} is outside the playbook's intraday bands (max 14).",
            score, grade,
        )

    path = route_path(
        has_directional_edge=has_directional_edge,
        is_range_bound=is_range_bound,
        iv_rank=iv_rank,
        directional_score=score,
    )

    # §16 hard rule, applied to BOTH paths.
    if score < MINIMUM_TRADEABLE_SCORE:
        return TradeRefusal(
            f"Signal score {score}/{MAX_SCORE} is below the {MINIMUM_TRADEABLE_SCORE} "
            f"minimum (§16); grade={grade.value}.",
            score, grade,
        )
    if path is StrategyPath.NO_TRADE:
        return TradeRefusal(
            "Regime router found neither a directional edge nor a "
            "range-plus-rich-IV condition (§10A).",
            score, grade,
        )

    if path is StrategyPath.REVERSAL:
        kind = StructureKind.BULL_CALL if bullish else StructureKind.BEAR_PUT
        is_debit = True
    else:
        kind = StructureKind.BULL_PUT if bullish else StructureKind.BEAR_CALL
        is_debit = False

    # §2 IV filter: buy premium cheap, sell it rich. Checked against the
    # structure actually chosen, not the one the caller expected.
    if not iv_regime_appropriate(iv_rank=iv_rank, is_debit=is_debit):
        return TradeRefusal(
            f"IV rank {iv_rank:.2f} is wrong for a "
            f"{'debit' if is_debit else 'credit'} structure (§2).",
            score, grade,
        )

    dte_years = _time_to_expiry_years(dte_days, zero_dte_hours_remaining)
    long_strike, short_strike = select_strikes(
        kind=kind, spot=spot, dte_years=dte_years, volatility=volatility,
        strike_increment=strike_increment,
    )

    try:
        spread = build_vertical(
            kind=kind, spot=spot, long_strike=long_strike, short_strike=short_strike,
            dte_years=dte_years, volatility=volatility,
        )
    except ValueError as exc:
        # build_vertical refuses anything not genuinely defined-risk.
        return TradeRefusal(f"Structure rejected: {exc}", score, grade)

    risk_pct = risk_budget_pct(grade, dte_band)
    budget = account_equity * risk_pct
    contracts = contracts_for_risk(account_risk=budget, spread=spread)
    if contracts < 1:
        return TradeRefusal(
            f"Risk budget {budget:.2f} is smaller than one contract's max loss "
            f"{spread.max_loss:.2f}; sizing to zero rather than exceeding risk.",
            score, grade,
        )

    return OptionTradePlan(
        path=path,
        spread=spread,
        contracts=contracts,
        score=score,
        grade=grade,
        risk_budget=budget,
        dte_band=dte_band,
        notes={
            "structure": kind.value,
            "direction": "bullish" if bullish else "bearish",
            "iv_rank": f"{iv_rank:.2f}",
            "priced": "MODEL (Black-Scholes) — re-price against real quotes before submitting",
        },
    )
