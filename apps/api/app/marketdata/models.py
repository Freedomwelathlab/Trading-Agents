from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class MarketSnapshot(BaseModel):
    """A normalized quote from exactly one vendor at one point in time. This
    is what feeds TradeProposal.estimated_price / market_data_as_of - never
    construct those fields from anything other than a real MarketSnapshot
    (spec Sec57: no fabricated prices)."""

    symbol: str = Field(min_length=1)
    price: Decimal = Field(gt=0)
    as_of: datetime
    source: str = Field(min_length=1)
    """Which provider produced this - kept on the snapshot itself so a
    rejected/approved trade's audit trail can show where the price actually
    came from, not just that a price existed."""

    @field_validator("as_of")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        return v
