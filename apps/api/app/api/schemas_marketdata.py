from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from apps.api.app.marketdata.bar_provider import BarInterval


class QuoteResponse(BaseModel):
    symbol: str
    price: Decimal
    as_of: datetime
    source: str


class BarResponse(BaseModel):
    """One persisted OHLCV bar, exactly as ingested (Phase 70, D088).

    `open`/`high`/`low`/`volume` are nullable because the underlying column
    is - a vendor may return a close-only record - and they are passed
    through as `null` rather than being backfilled from the close. A chart
    that receives a null high must draw nothing there; drawing a candle
    whose body and wicks were all derived from one number would render
    fabricated market structure.

    `source` travels with every bar so a suspicious value on a chart can be
    traced to the vendor that produced it, the same reasoning
    `Bar.source` and `MarketSnapshot.source` already state.
    """

    ts: datetime
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal
    volume: int | None
    source: str


class BarsResponse(BaseModel):
    """The bars this system HAS for a symbol and interval over a window -
    never the bars it was asked for.

    An empty `bars` list with a 200 is a real, expected answer meaning "no
    bars are ingested here", and is deliberately not a 404: the symbol may
    be perfectly valid and simply un-backfilled, and the two are different
    problems with different fixes. `count` is stated explicitly so a client
    does not have to infer "we got fewer than we asked for" from a date
    range and a weekend calendar it does not have.
    """

    symbol: str
    bar_interval: BarInterval
    count: int
    bars: list[BarResponse]


class DepthLevelResponse(BaseModel):
    """One priced level. Every level that reaches the wire has a real
    price - levels the vendor returned without one are dropped at the
    adapter, never sent as a zero (Phase 74, D092)."""

    price: Decimal
    volume: int
    order_count: int | None = None


class OrderBookResponse(BaseModel):
    """Both sides of the book, best price first.

    `spread` is null unless BOTH sides are present. A one-sided book has
    no spread, and a substituted zero is a number a reader would act on.
    """

    symbol: str
    bids: list[DepthLevelResponse]
    asks: list[DepthLevelResponse]
    spread: Decimal | None
    as_of: datetime
    source: str


class SessionLevelsResponse(BaseModel):
    """The session-anchored locations an intraday chart is read against
    (Phase 74, D092).

    Every field is nullable and a null means "the stored bars do not
    support this level", never zero. The first session in a window has no
    previous day; a session whose premarket never traded has no premarket
    high. A zero previous-low is below every price, which would make
    "price swept the previous low" permanently true.
    """

    symbol: str
    bar_interval: str
    session_date: date
    previous_high: Decimal | None
    previous_low: Decimal | None
    previous_close: Decimal | None
    premarket_high: Decimal | None
    premarket_low: Decimal | None
    opening_range_high: Decimal | None
    opening_range_low: Decimal | None
    regular_open: Decimal | None
    vwap: Decimal | None
    vwap_upper_2sigma: Decimal | None
    vwap_lower_2sigma: Decimal | None
    bars_in_session: int


class ChartSignal(BaseModel):
    """One B/S marker for the chart (Phase 85, D102).

    `score` and `evidence` are the setup's own, so the hover box on the
    chart shows the reasoning that produced the marker rather than a
    label. `stop_price` is the setup's structural invalidation; it is what
    makes the marker a proposal rather than an opinion.

    Every marker is produced by replaying the SAME detectors the backtest
    and the Autotrade bot use, on the same stored bars, with no
    look-ahead: a detector only ever sees bars up to and including the one
    it fires on.
    """

    ts: datetime
    setup: str
    side: str
    """`B` or `S` — what the chart draws."""
    direction: str
    price: Decimal
    stop_price: Decimal
    score: int
    evidence: dict[str, str]
    phase: str = "regular"
    """Session phase the marker fired in: `pre_market`, `regular` or
    `after_hours` (Phase 98 - the chart now scans the whole day)."""
    confidence: Decimal | None = None
    """Measured win rate (%) of THIS kind of signal - same setup, direction
    and score - over the calibration window: a win is +1R before the stop,
    within the session. None when fewer than 10 such signals resolved;
    never a figure from a handful of trades."""
    confidence_sample: int = 0
    """How many resolved signals `confidence` was measured on."""


class ChartSignalsResponse(BaseModel):
    symbol: str
    bar_interval: str
    setups: list[str]
    """Which detectors ran."""
    signals: list[ChartSignal]
    bars_scanned: int
    sessions: int
    note: str
    """Stated on every response: these markers are measurement, not
    advice. See docs/RESEARCH_5M.md."""
    market_type: str = "auto"
    calibration_sessions: int = 0
    """Sessions the confidence figures were measured over."""
    found: int = 0
    """Signals the detectors produced in the window BEFORE the score and
    confidence filters - so an empty chart says whether nothing fired or
    everything was filtered out."""
    max_score_found: int | None = None
    max_confidence_found: Decimal | None = None


class PhaseMoveResponse(BaseModel):
    """One session phase's movement (Phase 86, D105).

    `change`/`change_pct` are nullable on purpose and `reference_label`
    always names what the change was measured against, so a client never
    has to assume which close a percentage refers to.
    """

    phase: str
    bars: int
    first: Decimal
    last: Decimal
    high: Decimal
    low: Decimal
    volume: int | None = None
    reference: Decimal | None = None
    reference_label: str
    change: Decimal | None = None
    change_pct: Decimal | None = None


class ExtendedHoursResponse(BaseModel):
    symbol: str
    bar_interval: str
    session_date: date
    pre_market: PhaseMoveResponse | None = None
    regular: PhaseMoveResponse | None = None
    after_hours: PhaseMoveResponse | None = None
    note: str


class OptionExpiriesResponse(BaseModel):
    symbol: str
    expiries: list[date]
    source: str


class OptionQuoteResponse(BaseModel):
    """One contract. Every market field is nullable and a null means the
    vendor reported nothing - never a price of zero (Phase 89, D108)."""

    contract_symbol: str
    strike: Decimal
    right: str
    last_price: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    mid: Decimal | None = None
    spread: Decimal | None = None
    volume: int | None = None
    open_interest: int | None = None
    implied_vol: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None


class OptionChainResponse(BaseModel):
    symbol: str
    expiry: date
    as_of: datetime
    source: str
    calls: list[OptionQuoteResponse]
    puts: list[OptionQuoteResponse]
    quoted_contracts: int
    """How many of the listed contracts came back with any market at all.
    A chain of 300 rows where this is 12 is a chain nobody should trade
    from, and the number says so rather than leaving it to be inferred
    from a table of dashes."""
    note: str
