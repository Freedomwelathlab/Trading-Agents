"""The option strategy planner, over HTTP (Phase 91, D110).

Phase 78 (D096) built the playbook's decision rules as pure functions:
score the evidence, apply the hard floor, route the regime, choose a
structure, size it against a risk budget, or refuse. Nine hundred-odd
tested lines with no way to reach them from the application. This is that
way.

The endpoint plans, and never trades. It returns a `OptionTradePlan` or a
`TradeRefusal`, and a refusal is a 200 with `planned: false`, not an
error: "this signal does not clear the score floor" is the decision
layer working, and a 4xx would make a correct refusal look like a
malformed request.

**Every price in the response is MODELLED.** The spread is priced with
Black-Scholes from the volatility the caller supplies, because that is
what the Phase 78 layer does and this route does not silently upgrade it.
Nothing here has consulted the real chain, so the premium, the max
profit, the max loss and therefore the contract count are all statements
about a model. The response says so on every plan, in a field a client
cannot omit. Re-pricing against `GET /market-data/{symbol}/option-chain`
before submitting is the operator's job today and the obvious next
build — a modelled 1.85 against a market 2.10 is a third of the edge gone
before the first fill.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from apps.api.app.auth.dependencies import get_current_user
from apps.api.app.db.models import User
from apps.api.app.options.strategies import (
    MAX_SCORE,
    MINIMUM_TRADEABLE_SCORE,
    OptionTradePlan,
    SignalEvidence,
    TradeRefusal,
    plan_option_trade,
)
from apps.api.app.options.structures import Leg

router = APIRouter(prefix="/options", tags=["options"])


PRICING_NOTE = (
    "Every premium here is MODELLED (Black-Scholes, from the volatility supplied with the "
    "request). No real quote has been consulted, so the net premium, the max profit and loss, "
    "and therefore the contract count are statements about a model rather than about the "
    "market. Re-price the two legs against GET /market-data/{symbol}/option-chain before "
    "submitting anything."
)


class EvidenceRequest(BaseModel):
    """The §5 evidence table. Named flags rather than a score, so a plan
    records WHICH evidence produced its number — a 9 built from a sweep
    plus a structure shift is a different trade from a 9 built from four
    soft confirmations."""

    liquidity_sweep: bool = False
    market_structure_shift: bool = False
    cross_market_confirm: bool = False
    vwap_location: bool = False
    rsi_divergence: bool = False
    volume_confirm: bool = False
    atr_confirm: bool = False
    major_level: bool = False
    option_liquidity_ok: bool = False
    iv_appropriate: bool = False


class PlanRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    """Carried through to the response for the operator's benefit. The
    planner itself is symbol-agnostic: every input it uses is supplied
    below, which is also why nothing here can claim to have checked this
    symbol's real chain."""
    bullish: bool
    evidence: EvidenceRequest = Field(default_factory=EvidenceRequest)
    spot: Decimal = Field(gt=0)
    dte_days: int = Field(ge=0, le=60)
    volatility: Decimal = Field(gt=0, le=5)
    """Annualised, as a fraction: 0.45 is 45%."""
    iv_rank: Decimal = Field(ge=0, le=1)
    account_equity: Decimal = Field(gt=0)
    has_directional_edge: bool = True
    is_range_bound: bool = False
    strike_increment: Decimal = Field(default=Decimal("1"), gt=0)


class LegResponse(BaseModel):
    right: str
    strike: Decimal
    is_long: bool
    modeled_price: Decimal
    """Named for what it is. The field is `modeled_price` in the domain
    object and stays that in the response — renaming it to `premium` here
    would drop the one word that says nobody has checked the market."""


class SpreadResponse(BaseModel):
    kind: str
    long_leg: LegResponse
    short_leg: LegResponse
    width: Decimal
    net_premium: Decimal
    """Signed from the account's view: positive is a debit paid out,
    negative a credit received."""
    is_debit: bool
    max_profit: Decimal
    max_loss: Decimal
    breakeven: Decimal


class PlanResponse(BaseModel):
    symbol: str
    planned: bool
    score: int | None = None
    """Null only where a refusal happened before scoring could complete —
    `TradeRefusal` types both this and `grade` as optional, and forcing a
    number here would invent one."""
    max_score: int
    minimum_tradeable_score: int
    grade: str | None = None
    reason: str | None = None
    """Present only on a refusal, and it is the whole answer: which gate
    the signal failed and why."""
    path: str | None = None
    dte_band: str | None = None
    spread: SpreadResponse | None = None
    contracts: int | None = None
    risk_budget: Decimal | None = None
    max_loss_total: Decimal | None = None
    max_profit_total: Decimal | None = None
    notes: dict[str, str] = Field(default_factory=dict)
    pricing_note: str = PRICING_NOTE


def _leg(leg: Leg) -> LegResponse:
    return LegResponse(
        right=leg.right.value,
        strike=leg.strike,
        is_long=leg.is_long,
        modeled_price=leg.modeled_price,
    )


@router.post("/plan", response_model=PlanResponse)
async def plan(
    payload: PlanRequest,
    _current_user: User = Depends(get_current_user),
) -> PlanResponse:
    """Score a signal and either propose a defined-risk spread or refuse.

    A refusal is a 200 with `planned: false`. The decision layer refusing
    is it working — below the score floor, the wrong IV regime for the
    structure, a DTE outside the playbook's bands, or a risk budget too
    small for one contract — and a 4xx would make a correct answer look
    like a bad request.

    Read-only and side-effect free: it writes nothing, reaches no broker
    and places no order.
    """
    result = plan_option_trade(
        bullish=payload.bullish,
        evidence=SignalEvidence(**payload.evidence.model_dump()),
        spot=float(payload.spot),
        dte_days=payload.dte_days,
        volatility=float(payload.volatility),
        iv_rank=float(payload.iv_rank),
        account_equity=payload.account_equity,
        has_directional_edge=payload.has_directional_edge,
        is_range_bound=payload.is_range_bound,
        strike_increment=payload.strike_increment,
    )

    if isinstance(result, TradeRefusal):
        return PlanResponse(
            symbol=payload.symbol,
            planned=False,
            score=result.score,
            max_score=MAX_SCORE,
            minimum_tradeable_score=MINIMUM_TRADEABLE_SCORE,
            grade=result.grade.value if result.grade is not None else None,
            reason=result.reason,
        )

    assert isinstance(result, OptionTradePlan)  # nosec - the only other branch
    s = result.spread
    return PlanResponse(
        symbol=payload.symbol,
        planned=True,
        score=result.score,
        max_score=MAX_SCORE,
        minimum_tradeable_score=MINIMUM_TRADEABLE_SCORE,
        grade=result.grade.value,
        path=result.path.value,
        dte_band=result.dte_band.value,
        spread=SpreadResponse(
            kind=s.kind.value,
            long_leg=_leg(s.long_leg),
            short_leg=_leg(s.short_leg),
            width=s.width,
            net_premium=s.net_premium,
            is_debit=s.is_debit,
            max_profit=s.max_profit,
            max_loss=s.max_loss,
            breakeven=s.breakeven,
        ),
        contracts=result.contracts,
        risk_budget=result.risk_budget,
        max_loss_total=result.max_loss_total,
        max_profit_total=result.max_profit_total,
        notes=dict(result.notes),
    )
