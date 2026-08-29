"""Deterministic portfolio-level construction decisions. No LLM, no network
call, no I/O - the same discipline apps/api/app/risk/engine.py holds, for
the same structural reason (spec Sec3/Sec46/Sec62): position sizing is never
an LLM's output.

This is the "Portfolio Manager" box in docs/ARCHITECTURE.md's Target
diagram. It runs AFTER the Risk Engine has approved a proposal (spec Sec18:
"receives the Trade Proposal + Risk Report"; docs/TRADING_SAFETY.md's
pipeline puts the Portfolio Decision below the Risk Engine and above the
OMS), and it can only ever make a trade smaller or stop it - never larger,
never a different symbol, side, price or stop. When it does shrink one, the
OMS re-runs the Risk Engine on the modified proposal before any broker call
(apps/api/app/oms/service.py), so no quantity this component produces ever
reaches a broker ungated. That is the whole reason the sizing logic lives
here instead of inside the Risk Engine: the Risk Engine stays a pure
per-trade gate, and this component is one more thing it gates.

Why these three constraints and not spec Sec18's full list - see
apps/api/app/portfolio_manager/models.py's PortfolioConstraint docstring
and docs/DECISIONS.md D029.
"""

from decimal import Decimal

from apps.api.app.portfolio_manager.models import (
    PortfolioAction,
    PortfolioCheck,
    PortfolioConstraint,
    PortfolioDecision,
    PortfolioHolding,
    PortfolioLimits,
    PortfolioState,
)
from apps.api.app.risk.models import Side, TradeProposal


def decide(
    proposal: TradeProposal,
    portfolio: PortfolioState,
    limits: PortfolioLimits,
) -> PortfolioDecision:
    """`portfolio` must value the proposal's own symbol (if already held) at
    the same price the proposal carries, so that a projected post-trade
    value is `held_value + signed_quantity * estimated_price` exactly rather
    than a mix of two different marks. apps/api/app/api/routes/trades.py
    guarantees this: it passes the live quote as that symbol's mark and as
    `estimated_price` in the same call.

    Equity is measured pre-trade and used unchanged as the denominator for
    every percentage limit. That is not an approximation: buying converts
    cash into shares at `estimated_price` and selling does the reverse, so
    total equity is invariant across the trade at this price.

    Returns a PortfolioDecision on every path. Like the Risk Engine, a
    business-rule violation here is data, never an exception.
    """
    equity = portfolio.equity
    if equity <= 0:
        # Fail closed: every limit below is a fraction of equity, and a
        # zero/negative equity makes all of them meaningless rather than
        # generous. Never fall through to an approval.
        return PortfolioDecision(
            action=PortfolioAction.REJECT,
            requested_quantity=proposal.quantity,
            approved_quantity=Decimal(0),
            binding_constraint=None,
            detail=(
                f"Portfolio equity is {equity}; portfolio-level limits cannot be "
                "evaluated against non-positive equity."
            ),
            checks=[],
        )

    price = proposal.estimated_price
    signed_quantity = (
        proposal.quantity if proposal.side is Side.BUY else -proposal.quantity
    )

    checks = _evaluate(proposal.symbol, signed_quantity, price, portfolio, limits, equity)
    blocking = [c for c in checks if not c.passed and c.worsened_by_trade]
    if not blocking:
        return PortfolioDecision(
            action=PortfolioAction.APPROVE,
            requested_quantity=proposal.quantity,
            approved_quantity=proposal.quantity,
            binding_constraint=None,
            detail="No portfolio-level constraint is breached by this trade.",
            checks=checks,
        )

    # Only a BUY can worsen any of these three measures (a sell reduces the
    # symbol's value, raises cash, and can only reduce the position count),
    # so every blocking path below is a buy - and the maximum quantity that
    # would satisfy each blocking constraint is well defined.
    caps: list[tuple[PortfolioConstraint, Decimal]] = []
    for check in blocking:
        if check.constraint is PortfolioConstraint.SYMBOL_CONCENTRATION:
            headroom = limits.max_symbol_pct_of_equity * equity - portfolio.held_value(
                proposal.symbol
            )
            caps.append((check.constraint, _floor_quantity(max(headroom, Decimal(0)) / price)))
        elif check.constraint is PortfolioConstraint.CASH_RESERVE:
            headroom = portfolio.cash - limits.min_cash_reserve_pct_of_equity * equity
            caps.append((check.constraint, _floor_quantity(max(headroom, Decimal(0)) / price)))
        else:
            # MAX_OPEN_POSITIONS can only be worsened by opening a symbol
            # that isn't held yet, and there is no partial way to open a
            # position - any quantity >= 1 adds exactly one symbol.
            caps.append((check.constraint, Decimal(0)))

    # Deterministic tie-break: the smallest cap wins; equal caps resolve by
    # PortfolioConstraint declaration order, which `caps` already preserves
    # because `checks` is built in that order.
    binding_constraint, allowed = min(caps, key=lambda pair: pair[1])
    allowed = min(allowed, proposal.quantity)

    if allowed >= 1:
        return PortfolioDecision(
            action=PortfolioAction.MODIFY,
            requested_quantity=proposal.quantity,
            approved_quantity=allowed,
            binding_constraint=binding_constraint,
            detail=(
                f"Quantity reduced from {proposal.quantity} to {allowed} to satisfy "
                f"{binding_constraint.value}."
            ),
            checks=checks,
        )

    return PortfolioDecision(
        action=PortfolioAction.REJECT,
        requested_quantity=proposal.quantity,
        approved_quantity=Decimal(0),
        binding_constraint=binding_constraint,
        detail=(
            f"No quantity of at least 1 satisfies {binding_constraint.value}; "
            "the trade cannot be sized down into compliance."
        ),
        checks=checks,
    )


def _evaluate(
    symbol: str,
    signed_quantity: Decimal,
    price: Decimal,
    portfolio: PortfolioState,
    limits: PortfolioLimits,
    equity: Decimal,
) -> list[PortfolioCheck]:
    """Every constraint, evaluated at the REQUESTED quantity - the state of
    the world the decision was actually made about. Checks are not
    re-evaluated at a modified quantity, because the audit record is meant
    to explain why the request was changed, not to restate the outcome."""
    held_value = portfolio.held_value(symbol)
    projected_symbol_value = held_value + signed_quantity * price

    projected_cash = portfolio.cash - signed_quantity * price

    open_symbols = portfolio.open_symbols
    projected_symbols = set(open_symbols)
    if portfolio.held_quantity(symbol) + signed_quantity != 0:
        projected_symbols.add(symbol)
    else:
        projected_symbols.discard(symbol)

    symbol_limit = limits.max_symbol_pct_of_equity * equity
    cash_limit = limits.min_cash_reserve_pct_of_equity * equity
    count_limit = Decimal(limits.max_open_positions)
    projected_count = Decimal(len(projected_symbols))

    return [
        PortfolioCheck(
            constraint=PortfolioConstraint.SYMBOL_CONCENTRATION,
            passed=projected_symbol_value <= symbol_limit,
            projected_value=projected_symbol_value,
            limit_value=symbol_limit,
            worsened_by_trade=projected_symbol_value > held_value,
            detail=(
                f"Post-trade {symbol} value {projected_symbol_value} vs limit "
                f"{symbol_limit} ({limits.max_symbol_pct_of_equity:.0%} of "
                f"{equity} equity)."
            ),
        ),
        PortfolioCheck(
            constraint=PortfolioConstraint.CASH_RESERVE,
            passed=projected_cash >= cash_limit,
            projected_value=projected_cash,
            limit_value=cash_limit,
            worsened_by_trade=projected_cash < portfolio.cash,
            detail=(
                f"Post-trade cash {projected_cash} vs required reserve {cash_limit} "
                f"({limits.min_cash_reserve_pct_of_equity:.0%} of {equity} equity)."
            ),
        ),
        PortfolioCheck(
            constraint=PortfolioConstraint.MAX_OPEN_POSITIONS,
            passed=projected_count <= count_limit,
            projected_value=projected_count,
            limit_value=count_limit,
            worsened_by_trade=projected_count > Decimal(len(open_symbols)),
            detail=(
                f"Post-trade open positions {projected_count} vs limit {count_limit}."
            ),
        ),
    ]


def portfolio_state_from_positions(
    positions: dict[str, Decimal], marks: dict[str, Decimal], cash: Decimal
) -> PortfolioState:
    """Builds a PortfolioState from a broker adapter's raw positions and the
    caller's marks. Raises KeyError-equivalent ValueError for a held symbol
    with no mark rather than valuing it at zero or at a stale price - the
    same refusal PaperBrokerAdapter.get_account_state() makes (spec Sec57).
    Still pure: `positions` and `marks` are plain data the caller already
    holds, not something this module fetches."""
    holdings: list[PortfolioHolding] = []
    for symbol, quantity in positions.items():
        if quantity == 0:
            continue
        if symbol not in marks:
            raise ValueError(
                f"No mark supplied for open position {symbol}; cannot value portfolio."
            )
        holdings.append(
            PortfolioHolding(
                symbol=symbol, quantity=quantity, market_value=quantity * marks[symbol]
            )
        )
    return PortfolioState(cash=cash, holdings=holdings)


def _floor_quantity(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding="ROUND_FLOOR")
