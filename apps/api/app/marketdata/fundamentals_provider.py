"""Company-fundamentals port (D059) - the third market-data capability
alongside MarketDataProvider (one live quote, D008/D015) and
HistoryProvider (a price series, D021). Deliberately its own Protocol for
the same reason HistoryProvider is: "company financials" is a different
capability from "a price", not an overload of get_snapshot().

Same no-fabrication posture as marketdata/provider.py and
marketdata/history_provider.py: implementing this Protocol is the only
sanctioned way to add a source of fundamentals, its absence must render
as NOT_CONFIGURED, and a symbol the vendor has nothing for must raise
DataUnavailableError - reused from marketdata/provider.py rather than a
parallel set of error types, since the failure semantics are identical.

Every field on CompanyFundamentals is `| None` on purpose. A real vendor
routinely has a PE but no PS for the same symbol, and the whole point of
this phase is that a missing number stays missing all the way to the
prompt rather than being interpolated, defaulted to zero, or estimated
by an LLM (spec Sec57).
"""

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, Field, field_validator


class CompanyFundamentals(BaseModel):
    """Normalized, real fundamentals for exactly one symbol from exactly
    one vendor. Carries no derived/computed field: anything computable
    from these numbers (e.g. earnings yield from PE) is produced by the
    pure functions in apps/api/app/marketdata/fundamental_metrics.py, so
    this stays a faithful record of what the vendor actually returned."""

    symbol: str = Field(min_length=1)
    source: str = Field(min_length=1)
    """Which provider produced this - same audit-trail reasoning as
    MarketSnapshot.source."""

    company_name: str | None = None
    category: str | None = None
    """The vendor's own company-classification string, verbatim. Not
    normalized to any sector taxonomy - we don't have one, and inventing
    a mapping would be fabrication dressed up as normalization."""

    pe: Decimal | None = None
    pb: Decimal | None = None
    ps: Decimal | None = None
    dividend_yield: Decimal | None = None

    as_of: datetime | None = None
    """Timestamp of the most recent valuation data point actually
    returned. None when the vendor gave no dated valuation point at all -
    never filled in with "now", which would misrepresent stale data as
    fresh."""

    @field_validator("as_of")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        return v

    def has_any_data(self) -> bool:
        """True when the vendor returned at least one real field. A
        response where everything is None is not "fundamentals with
        unknown values" - it is no data, and callers must treat it as
        DataUnavailableError rather than narrate a row of blanks."""
        return any(
            value is not None
            for value in (
                self.company_name,
                self.category,
                self.pe,
                self.pb,
                self.ps,
                self.dividend_yield,
            )
        )


class FundamentalsProvider(Protocol):
    name: str

    async def get_fundamentals(self, symbol: str) -> CompanyFundamentals:
        """Raises DataUnavailableError when this vendor has nothing for
        the symbol (an unlisted/unsupported ticker), VendorError when the
        vendor itself failed. Never returns a partially-guessed record."""
        ...
