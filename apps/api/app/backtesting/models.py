"""Pydantic request/response models for the backtest endpoint. Every money/
quantity field is Decimal, never float - project-wide rule for anything
financial (same as portfolio/models.py, risk/models.py)."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator


class BacktestRequest(BaseModel):
    symbol: str = Field(min_length=1)
    start_date: date
    end_date: date
    """Must equal the current UTC calendar date - see
    docs/DECISIONS.md D025 / errors.UnsupportedDateRangeError's docstring
    for why an arbitrary historical end_date can't be honestly serviced by
    HistoryProvider's 'most recent N closes as of now' contract."""
    starting_cash: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def _validate_range(self) -> "BacktestRequest":
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


class EquityPoint(BaseModel):
    date: date
    equity: Decimal


class BacktestResult(BaseModel):
    symbol: str
    start_date: date
    end_date: date
    starting_cash: Decimal
    final_equity: Decimal
    total_return_pct: Decimal
    """(final_equity - starting_cash) / starting_cash * 100."""
    num_trades: int
    """Count of completed round trips (a buy that opens a flat position,
    followed by the sell that fully closes it again) - not raw fill count.
    Win rate is naturally defined against round trips, not individual
    fills, so the two numbers stay consistent with each other."""
    win_rate_pct: Decimal
    """Percentage of completed round trips where the closing sell price
    exceeded the (quantity-weighted) average entry price. Decimal(0) when
    num_trades is 0 - never a fabricated or undefined rate."""
    max_drawdown_pct: Decimal
    """Largest peak-to-trough decline in the equity curve, as a positive
    percentage of the peak at that point."""
    equity_curve: list[EquityPoint]
    """One point per trading day in [start_date, end_date], computed via
    the real PaperBrokerAdapter's cash+marks math (D014) at that day's
    close - never interpolated or estimated between days."""
