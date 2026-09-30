"""How an option order passes the deterministic Risk Engine (Phase 102, D122).
Pure: no I/O, no LLM.

**Opening orders go through `risk.engine.evaluate_trade` itself** - the same
function every equity order passes - not a parallel options engine. The
structure is presented to it in defined-risk terms:

    symbol          the structure's identity (underlying|type|expiry|legs)
    quantity        contracts
    estimated_price max loss per contract (the capital it reserves)
    stop_price      None - a defined-risk structure's stop IS its max loss
    market_data_as_of  the chain quote's own timestamp

and an `AccountState` whose `current_exposure` is the capital already
reserved by open option structures plus the market value of any equity
positions on the same broker. So the engine's position-size check bounds
one structure's capital at risk, its portfolio-exposure check is the "max
open risk" ceiling across every open position, and its buying-power check
refuses a structure the free cash cannot collateralise. Emergency stop,
duplicate-order and data-freshness checks run unchanged.

Two settings differ from an equity order, both deliberately:

- `require_stop_price` is False (the D035 precedent): there is no stop
  price on a spread, and demanding one would refuse every order.
- `max_market_data_age_seconds` is `OPTIONS_MAX_QUOTE_AGE_SECONDS` (1800):
  the only chain this account can read is ~15 minutes delayed.

**The one rule the engine cannot see** is per-trade risk: it computes that
from a stop distance, and a structure has none. Its max loss is its risk,
exactly, so it is checked here against the SAME
`max_risk_pct_of_equity_per_trade` limit and reported with the SAME
`BlockReason.EXCEEDS_PER_TRADE_RISK`, with the contract count that would
fit.

**Closing orders reduce risk**, so the sizing checks do not apply to them -
a limit that could trap a position open is a limit working against itself
(the D087 reasoning for exits). They still stop for the emergency stop and
for stale data, with the same block reasons.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from apps.api.app.risk.engine import evaluate_trade
from apps.api.app.risk.models import (
    AccountState,
    BlockReason,
    RecentOrder,
    RiskDecision,
    RiskLimits,
    Side,
    TradeProposal,
)


def option_limits(limits: RiskLimits, *, max_quote_age_seconds: int) -> RiskLimits:
    """The paper `RiskLimits` with the two option-specific overrides."""
    return limits.model_copy(
        update={
            "require_stop_price": False,
            "max_market_data_age_seconds": max_quote_age_seconds,
        }
    )


def evaluate_option_open(
    *,
    structure_key: str,
    quantity: int,
    max_loss_per_contract: Decimal,
    account: AccountState,
    limits: RiskLimits,
    emergency_stop_active: bool,
    now: datetime,
    quote_as_of: datetime,
    recent_orders: list[RecentOrder] | None = None,
) -> RiskDecision:
    """`limits` must already be `option_limits(...)`."""
    if quantity <= 0 or max_loss_per_contract <= 0:
        return RiskDecision(
            approved=False,
            reason=BlockReason.INVALID_PROPOSAL,
            detail="An option order needs a positive quantity and a positive max loss.",
        )
    proposal = TradeProposal(
        symbol=structure_key,
        side=Side.BUY,
        quantity=Decimal(quantity),
        estimated_price=max_loss_per_contract,
        stop_price=None,
        market_data_as_of=quote_as_of,
    )
    decision = evaluate_trade(
        proposal,
        account,
        limits,
        emergency_stop_active=emergency_stop_active,
        now=now,
        recent_orders=recent_orders,
    )
    if not decision.approved:
        return decision

    risk_capital = account.equity * limits.max_risk_pct_of_equity_per_trade
    total_risk = max_loss_per_contract * quantity
    if total_risk > risk_capital:
        fits = (risk_capital / max_loss_per_contract).to_integral_value(rounding=ROUND_FLOOR)
        return RiskDecision(
            approved=False,
            reason=BlockReason.EXCEEDS_PER_TRADE_RISK,
            detail=(
                f"Max loss {total_risk} ({quantity} x {max_loss_per_contract}) exceeds "
                f"{limits.max_risk_pct_of_equity_per_trade:.2%} of equity ({risk_capital}). "
                f"Contracts that fit: {fits}."
            ),
            max_quantity_allowed=fits,
        )
    return decision


def evaluate_option_close(
    *,
    limits: RiskLimits,
    emergency_stop_active: bool,
    now: datetime,
    quote_as_of: datetime,
) -> RiskDecision:
    """The fail-closed gates a risk-REDUCING order still passes."""
    if emergency_stop_active:
        return RiskDecision(
            approved=False,
            reason=BlockReason.EMERGENCY_STOP_ACTIVE,
            detail="Emergency stop is active; no trade can be approved.",
        )
    age = (now - quote_as_of).total_seconds()
    if age < 0:
        return RiskDecision(
            approved=False,
            reason=BlockReason.INVALID_PROPOSAL,
            detail="The quote's timestamp is in the future relative to evaluation time.",
        )
    if age > limits.max_market_data_age_seconds:
        return RiskDecision(
            approved=False,
            reason=BlockReason.MARKET_DATA_STALE,
            detail=(
                f"The option quote is {age:.0f}s old, exceeding the "
                f"{limits.max_market_data_age_seconds}s limit."
            ),
        )
    return RiskDecision(approved=True)
