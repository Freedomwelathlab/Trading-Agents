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
    MarketRiskInputs,
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
    market_risk: MarketRiskInputs | None = None,
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

    `market_risk` (Phase 62, D079) carries pre-computed per-symbol
    annualized volatility and pairwise correlation from `market_data_bars`.
    It is optional and defaults to `None`: every backtest caller
    (`apps/api/app/backtesting/engine.py`) omits it and the two market-risk
    checks then never run, so no existing backtest result moves. The trade
    path (`apps/api/app/api/routes/trades.py`) builds one and passes it.
    When it is supplied, the `PORTFOLIO_VOLATILITY` /
    `POSITION_CORRELATION` checks still run only if the matching
    `PortfolioLimits` field is set, and each SKIPS (audited, non-binding)
    rather than guessing when a symbol it needs has too little bar history.

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

    checks = _evaluate(
        proposal.symbol, signed_quantity, price, portfolio, limits, equity, market_risk
    )
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
        elif check.constraint is PortfolioConstraint.PORTFOLIO_VOLATILITY:
            # Projected book volatility rises monotonically with the traded
            # weight over [0, requested], so the largest integer quantity
            # that keeps it at or under the limit is found by bisection -
            # no closed form is attempted, the search is obviously correct.
            assert market_risk is not None and limits.max_portfolio_volatility_pct is not None
            caps.append(
                (
                    check.constraint,
                    _max_quantity_under_volatility(
                        proposal.symbol,
                        proposal.quantity,
                        price,
                        portfolio,
                        equity,
                        market_risk,
                        limits.max_portfolio_volatility_pct,
                    ),
                )
            )
        else:
            # MAX_OPEN_POSITIONS and POSITION_CORRELATION can only be
            # worsened by OPENING a symbol that isn't held yet, and there is
            # no partial way to open a position - any quantity >= 1 adds
            # exactly one symbol, at the same (quantity-independent)
            # correlation. Both are REJECT-or-allow, never sized down.
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
    market_risk: MarketRiskInputs | None,
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

    checks = [
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

    if market_risk is not None and limits.max_portfolio_volatility_pct is not None:
        checks.append(
            _volatility_check(
                symbol, signed_quantity, price, portfolio, equity,
                market_risk, limits.max_portfolio_volatility_pct,
            )
        )
    if market_risk is not None and limits.max_position_correlation is not None:
        checks.append(
            _correlation_check(
                symbol, signed_quantity, portfolio,
                market_risk, limits.max_position_correlation,
            )
        )
    return checks


def _projected_weights(
    symbol: str,
    traded_quantity: Decimal,
    price: Decimal,
    portfolio: PortfolioState,
    equity: Decimal,
) -> dict[str, Decimal]:
    """Post-trade market-value weight per held symbol, for `traded_quantity`
    shares of `symbol` bought at `price`. Equity is invariant across the
    trade at this price (see `decide`), so it is the denominator throughout.
    A weight is clamped at 0 (a full sell leaves none; nothing here goes
    short)."""
    weights: dict[str, Decimal] = {}
    for held in portfolio.holdings:
        if held.quantity != 0:
            weights[held.symbol] = max(held.market_value, Decimal(0)) / equity
    traded_value = max(portfolio.held_value(symbol) + traded_quantity * price, Decimal(0))
    if traded_value > 0:
        weights[symbol] = traded_value / equity
    else:
        weights.pop(symbol, None)
    return weights


def _portfolio_volatility(
    weights: dict[str, Decimal], market_risk: MarketRiskInputs
) -> Decimal | None:
    """sqrt(sum_i sum_j w_i w_j sigma_i sigma_j rho_ij), rho_ii = 1. Returns
    `None` if any symbol with non-zero weight is not covered, or any needed
    pairwise correlation is missing - the caller records that as a SKIPPED
    check rather than treating an unknown as zero covariance."""
    symbols = [s for s, w in weights.items() if w != 0]
    if any(not market_risk.covered(s) for s in symbols):
        return None
    variance = Decimal(0)
    for i, sym_i in enumerate(symbols):
        vol_i = market_risk.volatility(sym_i)
        assert vol_i is not None  # covered() checked above
        for sym_j in symbols[i:]:
            vol_j = market_risk.volatility(sym_j)
            assert vol_j is not None
            rho = market_risk.correlation_between(sym_i, sym_j)
            if rho is None:
                return None
            term = weights[sym_i] * weights[sym_j] * vol_i * vol_j * rho
            variance += term if sym_i == sym_j else term * 2
    if variance <= 0:
        return Decimal(0)
    return variance.sqrt()


def _volatility_check(
    symbol: str,
    signed_quantity: Decimal,
    price: Decimal,
    portfolio: PortfolioState,
    equity: Decimal,
    market_risk: MarketRiskInputs,
    limit: Decimal,
) -> PortfolioCheck:
    current_vol = _portfolio_volatility(
        _projected_weights(symbol, Decimal(0), price, portfolio, equity), market_risk
    )
    projected_vol = _portfolio_volatility(
        _projected_weights(symbol, signed_quantity, price, portfolio, equity), market_risk
    )
    if projected_vol is None:
        return PortfolioCheck(
            constraint=PortfolioConstraint.PORTFOLIO_VOLATILITY,
            passed=False,
            projected_value=Decimal(0),
            limit_value=Decimal(0),
            worsened_by_trade=False,
            skipped=True,
            detail=(
                "Portfolio-volatility check skipped: fewer than "
                f"{market_risk.min_observations} overlapping daily returns in the last "
                f"{market_risk.lookback_days} days for at least one held symbol, so no "
                "covariance could be computed without fabricating one."
            ),
        )
    worsened = signed_quantity > 0 and current_vol is not None and projected_vol > current_vol
    return PortfolioCheck(
        constraint=PortfolioConstraint.PORTFOLIO_VOLATILITY,
        passed=projected_vol <= limit,
        projected_value=projected_vol,
        limit_value=limit,
        worsened_by_trade=worsened,
        detail=(
            f"Projected post-trade annualized book volatility {projected_vol:.4f} vs limit "
            f"{limit:.4f} (from {market_risk.lookback_days}-day daily returns)."
        ),
    )


def _correlation_check(
    symbol: str,
    signed_quantity: Decimal,
    portfolio: PortfolioState,
    market_risk: MarketRiskInputs,
    limit: Decimal,
) -> PortfolioCheck:
    other_symbols = [s for s in portfolio.open_symbols if s != symbol]
    opening = signed_quantity > 0 and portfolio.held_quantity(symbol) == 0

    if not other_symbols:
        return PortfolioCheck(
            constraint=PortfolioConstraint.POSITION_CORRELATION,
            passed=True,
            projected_value=Decimal(0),
            limit_value=limit,
            worsened_by_trade=False,
            detail="No other position to be correlated with.",
        )

    pairs = [
        (other, market_risk.correlation_between(symbol, other)) for other in other_symbols
    ]
    measured = [(other, rho) for other, rho in pairs if rho is not None]
    if not market_risk.covered(symbol) or not measured:
        return PortfolioCheck(
            constraint=PortfolioConstraint.POSITION_CORRELATION,
            passed=False,
            projected_value=Decimal(0),
            limit_value=Decimal(0),
            worsened_by_trade=False,
            skipped=True,
            detail=(
                "Position-correlation check skipped: not enough overlapping daily "
                f"returns in the last {market_risk.lookback_days} days between {symbol} "
                "and any held symbol to compute a correlation."
            ),
        )

    worst_symbol, worst_rho = max(measured, key=lambda pair: pair[1])
    return PortfolioCheck(
        constraint=PortfolioConstraint.POSITION_CORRELATION,
        passed=worst_rho <= limit,
        projected_value=worst_rho,
        limit_value=limit,
        # Correlation is quantity-independent, so only OPENING a new
        # position can breach it; adding to one already held cannot.
        worsened_by_trade=opening and worst_rho > limit,
        detail=(
            f"{symbol} max correlation with a held symbol is {worst_rho:.4f} "
            f"(vs {worst_symbol}) vs limit {limit:.4f}."
        ),
    )


def _max_quantity_under_volatility(
    symbol: str,
    requested_quantity: Decimal,
    price: Decimal,
    portfolio: PortfolioState,
    equity: Decimal,
    market_risk: MarketRiskInputs,
    limit: Decimal,
) -> Decimal:
    """Largest integer quantity in [0, requested] whose projected book
    volatility is <= limit, by bisection. Projected vol rises monotonically
    with the bought weight here (a BUY that reached this path worsened the
    measure), so a bisection on the pass/fail boundary is exact for integer
    quantities."""

    def ok(quantity: Decimal) -> bool:
        vol = _portfolio_volatility(
            _projected_weights(symbol, quantity, price, portfolio, equity), market_risk
        )
        # A quantity whose weights can't be evaluated is not a safe fallback.
        return vol is not None and vol <= limit

    if not ok(Decimal(0)):
        return Decimal(0)
    lo, hi = Decimal(0), _floor_quantity(requested_quantity)
    if ok(hi):
        return hi
    while hi - lo > 1:
        mid = _floor_quantity((lo + hi) / 2)
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo


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
