"""Domain models for the trade-path Portfolio Manager (spec Sec18, D029).

Kept separate from apps/api/app/risk/models.py on purpose: the Risk Engine
and the Portfolio Manager are two different gates with two different
vocabularies, and collapsing `BlockReason` and `PortfolioConstraint` into
one enum would make it impossible to tell from an audit row which component
stopped a trade.
"""

import enum
from decimal import Decimal

from pydantic import BaseModel, Field


class PortfolioAction(str, enum.Enum):  # noqa: UP042 (str mixin kept for interop)
    """Spec Sec18's four outcomes, verbatim.

    REQUEST_MORE_RESEARCH is present for spec fidelity but is never emitted
    by decide(): "this needs more research" is inherently a discretionary
    judgement, and D029 deliberately keeps this component deterministic and
    LLM-free. Nothing in this codebase produces it today, and callers must
    not assume it can appear - see docs/DECISIONS.md D029.
    """

    APPROVE = "approve"
    MODIFY = "modify"
    REJECT = "reject"
    REQUEST_MORE_RESEARCH = "request_more_research"


class PortfolioConstraint(str, enum.Enum):  # noqa: UP042 (str mixin kept for interop)
    """Every portfolio-level rule this component can evaluate. One of these
    (never a bare False) names whichever rule bound a MODIFY or a REJECT.

    Spec Sec18 also lists correlation, sector concentration, portfolio
    volatility, expected return and drawdown. None of those are here:
    this repo stores no sector/classification data on `assets`, and no
    persisted price series a covariance or a volatility could be computed
    from. Inventing any of them would be fabrication (docs/TRADING_SAFETY.md),
    so they are deferred rather than approximated - see D029.
    """

    SYMBOL_CONCENTRATION = "symbol_concentration"
    """Post-trade market value of THIS symbol as a share of total equity.
    Distinct from the Risk Engine's max_position_pct_of_equity, which
    measures a single proposal's notional - a series of individually
    compliant adds can still build an aggregate position no single
    per-trade check ever sees."""

    CASH_RESERVE = "cash_reserve"
    """Post-trade cash as a share of total equity. Distinct from the Risk
    Engine's INSUFFICIENT_BUYING_POWER check, which only asks whether cash
    covers this one notional at all (cash >= 0 afterwards)."""

    MAX_OPEN_POSITIONS = "max_open_positions"
    """Count of distinct symbols held post-trade - the crudest available
    proxy for spec Sec18's 'diversification', and honestly labelled as a
    count, not as a diversification measure."""


class PortfolioHolding(BaseModel):
    """One currently-held position, already valued at a real mark by the
    caller. This component never marks a position itself and never sources
    a price - it is handed deterministic values, exactly as the Risk Engine
    is handed AccountState (spec Sec62)."""

    symbol: str = Field(min_length=1)
    quantity: Decimal
    market_value: Decimal
    """quantity * mark. Signed the same way quantity is; the paper broker
    is long-only today, so this is >= 0 in practice."""


class PortfolioState(BaseModel):
    """Deterministic, pre-trade portfolio facts. Never sourced from an LLM."""

    cash: Decimal = Field(ge=0)
    holdings: list[PortfolioHolding] = Field(default_factory=list)

    def held_quantity(self, symbol: str) -> Decimal:
        return sum(
            (h.quantity for h in self.holdings if h.symbol == symbol), start=Decimal(0)
        )

    def held_value(self, symbol: str) -> Decimal:
        return sum(
            (h.market_value for h in self.holdings if h.symbol == symbol), start=Decimal(0)
        )

    @property
    def positions_value(self) -> Decimal:
        return sum((h.market_value for h in self.holdings), start=Decimal(0))

    @property
    def equity(self) -> Decimal:
        return self.cash + self.positions_value

    @property
    def open_symbols(self) -> set[str]:
        return {h.symbol for h in self.holdings if h.quantity != 0}


class PortfolioLimits(BaseModel):
    """No defaults here, for the same reason RiskLimits has none: an
    unconfigured portfolio limit is a configuration bug, not 'unlimited'.
    apps/api/app/core/config.py owns the actual numbers."""

    max_symbol_pct_of_equity: Decimal = Field(gt=0, le=1)
    min_cash_reserve_pct_of_equity: Decimal = Field(ge=0, lt=1)
    max_open_positions: int = Field(gt=0)


class PortfolioCheck(BaseModel):
    """One constraint, evaluated against the post-trade portfolio. Every
    check this component ran appears in PortfolioDecision.checks whether it
    passed or not - that list IS spec Sec18's 'complete audit record', so
    it must show what was considered, not only what failed."""

    constraint: PortfolioConstraint
    passed: bool
    projected_value: Decimal
    """The measured post-trade quantity (a market value, a cash balance, or
    a position count as a Decimal)."""
    limit_value: Decimal
    """The threshold projected_value was compared against."""
    worsened_by_trade: bool
    """True when the trade moves this measure in the disallowed direction.
    A failed check only blocks when this is also True: a portfolio that is
    already over-concentrated must not have a de-risking sell refused
    because of the very condition that sell would relieve."""
    detail: str


class PortfolioDecision(BaseModel):
    """The complete audit record spec Sec18 requires. Persisted per-order by
    apps/api/app/oms/persistence.py (orders.portfolio_* columns, migration
    0009)."""

    action: PortfolioAction
    requested_quantity: Decimal
    """The quantity the risk-approved proposal arrived with."""
    approved_quantity: Decimal
    """What this component will allow. Equals requested_quantity on
    APPROVE, is strictly smaller and >= 1 on MODIFY, and is 0 on REJECT.
    This component only ever shrinks a trade - it never proposes a larger
    one, and it never changes side, symbol, price or stop."""
    binding_constraint: PortfolioConstraint | None = None
    """Which constraint forced a MODIFY or a REJECT. None on APPROVE."""
    detail: str
    checks: list[PortfolioCheck]
