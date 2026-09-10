"""Domain models for the trade-path Portfolio Manager (spec Sec18, D029).

Kept separate from apps/api/app/risk/models.py on purpose: the Risk Engine
and the Portfolio Manager are two different gates with two different
vocabularies, and collapsing `BlockReason` and `PortfolioConstraint` into
one enum would make it impossible to tell from an audit row which component
stopped a trade.
"""

import enum
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator


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

    Spec Sec18 also lists sector concentration, expected return and
    drawdown. Those are still NOT here: this repo stores no
    sector/classification data on `assets`, and a persisted per-symbol
    expected-return or drawdown figure would be a forecast this platform
    does not make. Inventing any of them would be fabrication
    (docs/TRADING_SAFETY.md), so they stay deferred - see D029.

    Correlation and portfolio volatility WERE in that deferred list until
    Phase 62 (D079): `market_data_bars` (Phase 53, D070) now persists the
    daily price series a covariance and an annualized volatility can be
    computed from honestly. Those two checks run only when the caller
    supplies a `MarketRiskInputs` (the trade path does; every backtest
    caller passes `None`) AND the matching `PortfolioLimits` field is set -
    and each is SKIPPED, recorded as a non-passing-but-not-binding check
    with `worsened_by_trade=False`, whenever a symbol it would need has too
    little bar history, rather than proceeding on a fabricated number.
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

    PORTFOLIO_VOLATILITY = "portfolio_volatility"
    """Projected post-trade annualized volatility of the whole book
    (sqrt(wᵀ Σ w), w = post-trade market-value weights, Σ from
    `MarketRiskInputs`), as a fraction of 1.0, vs
    `PortfolioLimits.max_portfolio_volatility_pct`. Phase 62 (D079).

    Only ever worsened by a BUY that raises the weight of a name that adds
    variance to the book; a de-risking sell that lowers projected vol is
    never blocked by this (the `worsened_by_trade` rule). When it binds a
    MODIFY, the cap is found by bisection on integer quantity - projected
    vol is monotonic in the traded weight over the relevant range, so the
    largest quantity keeping the book at or under the limit is well
    defined."""

    POSITION_CORRELATION = "position_correlation"
    """The proposed symbol's correlation with the rest of the book - the
    maximum pairwise Pearson correlation (from `MarketRiskInputs`) between
    it and any currently-held OTHER symbol - vs
    `PortfolioLimits.max_position_correlation`. Phase 62 (D079).

    Correlation does not depend on quantity, so this check only ever blocks
    OPENING a new position that is too correlated with something already
    held; adding to a position already in the book cannot worsen it (that
    is the volatility check's job), and it is never sized down - the cap is
    0, exactly like MAX_OPEN_POSITIONS, so a blocking correlation is a
    REJECT, not a MODIFY."""


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
    """No defaults for the first three, for the same reason RiskLimits has
    none: an unconfigured portfolio limit is a configuration bug, not
    'unlimited'. apps/api/app/core/config.py owns the actual numbers.

    The last two (Phase 62, D079) DO default to `None`, and `None` here
    means exactly 'do not run this check' rather than 'unlimited' - the
    same meaning `market_risk=None` has one level up in `decide()`. They
    are optional because the volatility and correlation checks need a
    `MarketRiskInputs` that only the trade path builds; a caller that has
    no bar data to compute one (every backtest) leaves both unset and the
    two Phase-62 checks simply never appear in the decision. `config.py`
    always sets them for the trade path, so on that path they are only
    `None` in a test that deliberately omits them.
    """

    max_symbol_pct_of_equity: Decimal = Field(gt=0, le=1)
    min_cash_reserve_pct_of_equity: Decimal = Field(ge=0, lt=1)
    max_open_positions: int = Field(gt=0)
    max_portfolio_volatility_pct: Decimal | None = Field(default=None, gt=0)
    """Ceiling on projected post-trade annualized book volatility, as a
    fraction of 1.0 (e.g. 0.40 = 40% annualized). Checked only when a
    `MarketRiskInputs` is also supplied."""
    max_position_correlation: Decimal | None = Field(default=None, ge=0, le=1)
    """Ceiling on the proposed symbol's maximum pairwise correlation with
    any other held symbol, above which a NEW position is not opened.
    Checked only when a `MarketRiskInputs` is also supplied."""


class MarketRiskInputs(BaseModel):
    """Pre-computed, deterministic per-symbol volatility and pairwise
    correlation for the two Phase-62 constraints (D079). Built by the
    trade path from `market_data_bars` daily closes
    (apps/api/app/marketdata/portfolio_risk.py) and handed in as plain
    data - exactly like the Risk Engine's `recent_orders` and the
    emergency-stop boolean, so `decide()` stays zero-I/O (D029 rejected
    alternative (f): a market-data dependency inside the component).

    A symbol appears in `annualized_volatility` if and only if enough
    daily bars were ingested to compute it; `covered()` is the single
    check `decide()` uses to decide whether a symbol's risk numbers are
    real. A symbol the book holds but that is NOT covered makes the
    portfolio-volatility check skip (audited), never guess.

    `correlation` is a flattened upper-triangle map keyed by
    `"<A>|<B>"` with A < B lexicographically, so there is exactly one
    entry per unordered pair; use `correlation_between()` rather than
    indexing it directly. A pair with no entry (either symbol uncovered,
    or the two return series had no overlapping days) reads as `None`,
    not as 0.
    """

    annualized_volatility: dict[str, Decimal] = Field(default_factory=dict)
    correlation: dict[str, Decimal] = Field(default_factory=dict)
    lookback_days: int = Field(gt=0)
    """How many trailing calendar days of bars were requested to build
    this - carried through to the audit detail so a reader knows the
    window the numbers describe."""
    min_observations: int = Field(gt=1)
    """The minimum overlapping daily returns required for a symbol (or a
    pair) to be 'covered'; a symbol with fewer is absent from
    `annualized_volatility` (or the pair from `correlation`)."""

    @staticmethod
    def _pair_key(symbol_a: str, symbol_b: str) -> str:
        lo, hi = sorted((symbol_a, symbol_b))
        return f"{lo}|{hi}"

    def covered(self, symbol: str) -> bool:
        """True when `symbol` has a real, computed annualized volatility."""
        return symbol in self.annualized_volatility

    def volatility(self, symbol: str) -> Decimal | None:
        return self.annualized_volatility.get(symbol)

    def correlation_between(self, symbol_a: str, symbol_b: str) -> Decimal | None:
        """The Pearson correlation of the two daily-return series, or `None`
        when it could not be computed. A symbol with itself is 1."""
        if symbol_a == symbol_b:
            return Decimal(1) if self.covered(symbol_a) else None
        return self.correlation.get(self._pair_key(symbol_a, symbol_b))

    @model_validator(mode="after")
    def _correlations_are_in_range(self) -> "MarketRiskInputs":
        for key, value in self.correlation.items():
            if not (Decimal(-1) <= value <= Decimal(1)):
                raise ValueError(f"correlation[{key}] = {value} is outside [-1, 1]")
        if any(v < 0 for v in self.annualized_volatility.values()):
            raise ValueError("annualized_volatility has a negative entry")
        return self


class PortfolioCheck(BaseModel):
    """One constraint, evaluated against the post-trade portfolio. Every
    check this component ran appears in PortfolioDecision.checks whether it
    passed or not - that list IS spec Sec18's 'complete audit record', so
    it must show what was considered, not only what failed."""

    constraint: PortfolioConstraint
    passed: bool
    projected_value: Decimal
    """The measured post-trade quantity (a market value, a cash balance, or
    a position count as a Decimal). Zero on a `skipped` check, where it
    carries no meaning - read `skipped` first."""
    limit_value: Decimal
    """The threshold projected_value was compared against. Zero on a
    `skipped` check."""
    worsened_by_trade: bool
    """True when the trade moves this measure in the disallowed direction.
    A failed check only blocks when this is also True: a portfolio that is
    already over-concentrated must not have a de-risking sell refused
    because of the very condition that sell would relieve. Always False on
    a `skipped` check - a check that did not run cannot bind."""
    detail: str
    skipped: bool = False
    """Phase 62 (D079): the check was in scope (its limit was configured and
    a `MarketRiskInputs` was supplied) but could not be evaluated because a
    symbol it needed had too little ingested bar history. It is recorded -
    `passed=False`, `worsened_by_trade=False` - so the audit trail shows the
    check was considered and why it produced no verdict, rather than the
    trail being silent about a constraint that exists. Never set on the
    three D029 constraints, which need no external data."""


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
