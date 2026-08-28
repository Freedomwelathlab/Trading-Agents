"""Typed errors for backtest data availability. Same no-fabrication posture
as every other data-availability error in this codebase (marketdata's
DataUnavailableError/VendorError, portfolio's MissingMarkError/
BrokerAccountNotFoundError) - a gap in real data is always a typed error
the caller renders as DATA_UNAVAILABLE, never a shorter/padded/synthetic
series (docs/TRADING_SAFETY.md's no-fabrication rule)."""


class InsufficientHistoryError(Exception):
    """The vendor (HistoryProvider) had fewer daily closes available than
    this backtest window requires (the requested trading-day range plus the
    SMA(20) warmup period). Never padded, never shortened silently - the
    caller must surface this as a clear 400, exactly like
    marketdata.indicators.InsufficientDataError's identical discipline for
    a single indicator computation."""


class UnsupportedDateRangeError(Exception):
    """Raised when end_date is not the most recent available trading day
    (today, UTC). See docs/DECISIONS.md D025's HistoryProvider limitation
    note: HistoryProvider (D021) only exposes 'the most recent N daily
    closes as of now' - it has no method to fetch an arbitrary historical
    window that doesn't end at the present. Silently ignoring this and
    labeling recent data with the user's requested (older) end_date would
    mislabel real prices with wrong dates - a subtler form of the same
    fabrication problem InsufficientHistoryError guards against. Extending
    HistoryProvider itself is out of this phase's scope (a marketdata
    change, not a backtesting one) - noted as explicit future work."""
