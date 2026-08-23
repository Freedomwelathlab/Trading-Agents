import enum
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class Side(str, enum.Enum):  # noqa: UP042 (str mixin kept for interop)
    BUY = "buy"
    SELL = "sell"


class BlockReason(str, enum.Enum):  # noqa: UP042 (str mixin kept for interop)
    """Every rejection carries exactly one of these. Never a bare False."""

    EMERGENCY_STOP_ACTIVE = "emergency_stop_active"
    INVALID_PROPOSAL = "invalid_proposal"
    MARKET_DATA_STALE = "market_data_stale"
    MARKET_DATA_UNAVAILABLE = "market_data_unavailable"
    MISSING_STOP_PRICE = "missing_stop_price"
    INVALID_STOP_DISTANCE = "invalid_stop_distance"
    EXCEEDS_PER_TRADE_RISK = "exceeds_per_trade_risk"
    EXCEEDS_MAX_POSITION_SIZE = "exceeds_max_position_size"
    EXCEEDS_PORTFOLIO_EXPOSURE = "exceeds_portfolio_exposure"
    INSUFFICIENT_BUYING_POWER = "insufficient_buying_power"
    LIMITS_NOT_CONFIGURED = "limits_not_configured"


class TradeProposal(BaseModel):
    """What an (eventual) trader agent proposes. Never trusted as-is —
    every field here is validated by the engine, not assumed correct."""

    symbol: str = Field(min_length=1)
    side: Side
    quantity: Decimal = Field(gt=0)
    estimated_price: Decimal = Field(gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    market_data_as_of: datetime

    @field_validator("market_data_as_of")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("market_data_as_of must be timezone-aware")
        return v


class AccountState(BaseModel):
    """Deterministic account facts. Never sourced from an LLM (spec Sec62)."""

    equity: Decimal = Field(gt=0)
    cash: Decimal = Field(ge=0)
    current_exposure: Decimal = Field(ge=0)
    """Sum of abs(market value) of all open positions, before this trade."""


class RiskLimits(BaseModel):
    """No defaults — an unconfigured limit is a config bug, not 'unlimited'.
    Constructing this with sane numbers is a deliberate, reviewed act."""

    max_position_pct_of_equity: Decimal = Field(gt=0, le=1)
    max_portfolio_exposure_pct_of_equity: Decimal = Field(gt=0, le=1)
    max_risk_pct_of_equity_per_trade: Decimal = Field(gt=0, le=1)
    require_stop_price: bool = True
    max_market_data_age_seconds: int = Field(gt=0)


class RiskDecision(BaseModel):
    approved: bool
    reason: BlockReason | None = None
    detail: str | None = None
    max_quantity_allowed: Decimal | None = None
    """Informational only. The engine never resizes an order itself — a
    rejected proposal must come back as a new, explicit proposal."""
