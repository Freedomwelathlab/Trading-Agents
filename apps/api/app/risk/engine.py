"""Deterministic risk validation. No LLM, no network call, no I/O.

This module exists specifically so that spec Sec3/Sec46 hold structurally: an
LLM proposes, this module decides, and this module keeps working even if
every LLM provider in the system is down or actively raising. Nothing in
here imports an LLM client, and nothing here should ever be made to.

Every path returns a RiskDecision. Nothing raises for a business-rule
violation - a violation is data (a BlockReason), not an exception. The only
things that should raise are Pydantic validation errors on malformed input,
which the caller must treat as a block, not a crash to recover from.
"""

from datetime import UTC, datetime
from decimal import Decimal

from apps.api.app.risk.models import (
    AccountState,
    BlockReason,
    RiskDecision,
    RiskLimits,
    TradeProposal,
)


def evaluate_trade(
    proposal: TradeProposal,
    account: AccountState,
    limits: RiskLimits,
    *,
    emergency_stop_active: bool = False,
    now: datetime | None = None,
) -> RiskDecision:
    """Fail closed at every branch: the moment a rule can't be verified as
    satisfied, return a rejection immediately rather than falling through
    to an approval at the end of the function.
    """
    if emergency_stop_active:
        return RiskDecision(
            approved=False,
            reason=BlockReason.EMERGENCY_STOP_ACTIVE,
            detail="Emergency stop is active; no trade can be approved.",
        )

    now = now or datetime.now(UTC)
    data_age_seconds = (now - proposal.market_data_as_of).total_seconds()
    if data_age_seconds < 0:
        return RiskDecision(
            approved=False,
            reason=BlockReason.INVALID_PROPOSAL,
            detail="market_data_as_of is in the future relative to evaluation time.",
        )
    if data_age_seconds > limits.max_market_data_age_seconds:
        return RiskDecision(
            approved=False,
            reason=BlockReason.MARKET_DATA_STALE,
            detail=(
                f"Market data is {data_age_seconds:.0f}s old, exceeding the "
                f"{limits.max_market_data_age_seconds}s limit."
            ),
        )

    if limits.require_stop_price and proposal.stop_price is None:
        return RiskDecision(
            approved=False,
            reason=BlockReason.MISSING_STOP_PRICE,
            detail="This account's risk limits require a stop price on every proposal.",
        )

    stop_distance: Decimal | None = None
    if proposal.stop_price is not None:
        stop_distance = abs(proposal.estimated_price - proposal.stop_price)
        if stop_distance == 0:
            return RiskDecision(
                approved=False,
                reason=BlockReason.INVALID_STOP_DISTANCE,
                detail="stop_price equals estimated_price; stop distance cannot be zero.",
            )

    notional = proposal.quantity * proposal.estimated_price

    max_position_notional = account.equity * limits.max_position_pct_of_equity
    if notional > max_position_notional:
        return RiskDecision(
            approved=False,
            reason=BlockReason.EXCEEDS_MAX_POSITION_SIZE,
            detail=(
                f"Proposed notional {notional} exceeds the max single-position notional "
                f"{max_position_notional} ({limits.max_position_pct_of_equity:.0%} of equity)."
            ),
            max_quantity_allowed=_floor_quantity(max_position_notional / proposal.estimated_price),
        )

    projected_exposure = account.current_exposure + notional
    max_exposure = account.equity * limits.max_portfolio_exposure_pct_of_equity
    if projected_exposure > max_exposure:
        return RiskDecision(
            approved=False,
            reason=BlockReason.EXCEEDS_PORTFOLIO_EXPOSURE,
            detail=(
                f"Projected total exposure {projected_exposure} would exceed the "
                f"portfolio limit {max_exposure} "
                f"({limits.max_portfolio_exposure_pct_of_equity:.0%} of equity)."
            ),
        )

    if stop_distance is not None:
        risk_capital = account.equity * limits.max_risk_pct_of_equity_per_trade
        max_qty_by_risk = _floor_quantity(risk_capital / stop_distance)
        if proposal.quantity > max_qty_by_risk:
            return RiskDecision(
                approved=False,
                reason=BlockReason.EXCEEDS_PER_TRADE_RISK,
                detail=(
                    f"Quantity {proposal.quantity} risks more than "
                    f"{limits.max_risk_pct_of_equity_per_trade:.2%} of equity given a stop "
                    f"distance of {stop_distance}. Max quantity by risk: {max_qty_by_risk}."
                ),
                max_quantity_allowed=max_qty_by_risk,
            )

    if notional > account.cash:
        return RiskDecision(
            approved=False,
            reason=BlockReason.INSUFFICIENT_BUYING_POWER,
            detail=f"Notional {notional} exceeds available cash {account.cash}.",
        )

    return RiskDecision(approved=True)


def _floor_quantity(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding="ROUND_FLOOR")
