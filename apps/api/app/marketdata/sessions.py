"""US equity session structure, derived from real bars (Phase 73, D091).

Every strategy in the TQQQ intraday playbook is anchored to a location on
the session clock - the previous day's high, the premarket low, the first
fifteen minutes' range, the distance from session VWAP. None of those are
indicators computed from a rolling window; they are facts about WHERE in a
trading day a bar sits. This module is the one place that decides that.

Three rules hold everywhere below, and each exists because its opposite
produces a plausible-looking wrong answer rather than an error.

**1. Session phase is decided in America/New_York, never by a UTC offset.**
The regular session opens at 13:30 UTC for most of the year and 14:30 UTC
between November and March. A fixed-offset rule is therefore correct for
part of any multi-month backtest and silently an hour out for the rest -
which moves the "first fifteen minutes" onto the wrong bars for roughly
four months in twelve, without ever failing.

**2. Levels are derived from the bars present, never from an assumed
calendar.** No holiday table, no early-close list. A session that closed at
13:00 ET on Christmas Eve yields the levels its own bars support; a day
with no bars yields nothing at all. This is what keeps the module honest
when the data has gaps, and it means the ~9 early closes a year and every
market holiday are handled without a calendar to maintain and drift.

**3. "Previous day" means the previous session that HAS data, not
yesterday.** Calendar arithmetic gives a Monday a previous day of Sunday,
and the day after a holiday a previous day that never traded. Both would
return empty levels, and an empty PDH reads downstream as "price never
swept the level" - a silent no-trade rather than a visible gap.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from apps.api.app.marketdata.ohlcv import OHLCVBar

US_EQUITY_TZ = ZoneInfo("America/New_York")
"""The exchange's own clock. Every boundary below is a wall-clock time in
this zone, so DST is handled by the zone rather than by arithmetic."""

PREMARKET_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
AFTER_HOURS_CLOSE = time(20, 0)

DEFAULT_OPENING_RANGE_MINUTES = 15
"""The playbook's starting value, and explicitly flagged there as a value
to validate rather than assume (it names 30 minutes as the alternative to
test). Exposed as a parameter everywhere below for exactly that reason."""


class SessionPhase(str, enum.Enum):  # noqa: UP042 (str mixin matches this codebase)
    """Which part of the extended US equity day a bar belongs to.

    A bar is classified by the instant it OPENS. The bar stamped 09:30 is
    the first regular-hours bar and the one stamped 15:55 is the last, so a
    5-minute regular session is 78 bars - the count this module is verified
    against on real TQQQ data.
    """

    PRE_MARKET = "pre_market"
    REGULAR = "regular"
    AFTER_HOURS = "after_hours"


def to_exchange_time(ts: datetime) -> datetime:
    """An aware instant rendered on the exchange's clock.

    Raises on a naive datetime rather than assuming it is UTC. A naive
    timestamp reaching here is a bug in whatever produced it, and guessing
    a zone is precisely how the vendor's local-time candles were misread
    before (see `_as_utc` in providers/longbridge.py).
    """
    if ts.tzinfo is None:
        raise ValueError(
            f"Session classification needs an aware timestamp; got naive {ts!r}. "
            "Guessing a timezone here is what silently moves the US session."
        )
    return ts.astimezone(US_EQUITY_TZ)


def session_date(ts: datetime) -> date:
    """The exchange-local calendar date a bar trades on.

    This is why a UTC date is not usable as a session key: the regular
    session ends at 20:00 UTC in summer but the after-hours session runs to
    00:00 UTC, so a single trading day straddles two UTC dates. Grouping on
    the UTC date splits one session in half and files the tail under the
    next day.
    """
    return to_exchange_time(ts).date()


def session_phase(ts: datetime) -> SessionPhase | None:
    """`None` for an instant outside the 04:00-20:00 ET extended day, which
    is a real answer: overnight prints exist in some feeds and belong to no
    session's levels."""
    local = to_exchange_time(ts).time()
    if PREMARKET_OPEN <= local < REGULAR_OPEN:
        return SessionPhase.PRE_MARKET
    if REGULAR_OPEN <= local < REGULAR_CLOSE:
        return SessionPhase.REGULAR
    if REGULAR_CLOSE <= local < AFTER_HOURS_CLOSE:
        return SessionPhase.AFTER_HOURS
    return None


def is_regular_hours(ts: datetime) -> bool:
    """The gate an intraday strategy runs behind.

    Premarket and after-hours bars are stored because the playbook prices
    premarket highs and lows, but executing against them assumes fills at a
    spread that does not exist: TQQQ trades roughly 53M shares in the
    regular session against 1.9M premarket, measured over the 124-session
    window this was built on. A backtest that fills at 04:05 is reporting
    on a book it could not have traded.
    """
    return session_phase(ts) is SessionPhase.REGULAR


# ---------------------------------------------------------------------------
# Session calendars (Phase 103, D123)
#
# Everything above is the US equity clock, and every function below that
# groups, levels or anchors bars used to call it directly. Spot FX has a
# different clock entirely - one continuous session from Sunday 17:00 New
# York to Friday 17:00 New York, rolling each day at 17:00 NY - and running
# FX bars through the equity clock does not fail: it silently files a
# 20:00 NY print under the wrong day, calls 04:00 NY "pre-market" and ends
# every session at 16:00. So the clock is now an object the session
# functions take, defaulting to the equity one, so every existing caller is
# byte-for-byte unchanged.
# ---------------------------------------------------------------------------


class SessionCalendar:
    """The questions the session functions ask of a market's clock.

    `causal_opening_range` says whether `build_session_levels` stamps the
    instant a session's opening range becomes KNOWN, so the intraday engine
    can hide it from bars that print before then. It is False for the US
    equity calendar ONLY to keep every existing equity result reproducible;
    see D123 for why that is flagged as a known look-ahead in the equity
    path rather than silently changed here.
    """

    name: str = "abstract"
    causal_opening_range: bool = False

    def session_date(self, ts: datetime) -> date:  # pragma: no cover - interface
        raise NotImplementedError

    def session_phase(self, ts: datetime) -> SessionPhase | None:  # pragma: no cover
        raise NotImplementedError

    def is_regular_hours(self, ts: datetime) -> bool:
        return self.session_phase(ts) is SessionPhase.REGULAR

    def regular_open_at(self, day: date) -> datetime:  # pragma: no cover - interface
        """The instant the tradable part of session `day` begins, which is
        what the opening range is anchored to."""
        raise NotImplementedError


class UsEquityCalendar(SessionCalendar):
    """The module-level functions above, as a calendar. Nothing new."""

    name = "us_equity"
    causal_opening_range = False

    def session_date(self, ts: datetime) -> date:
        return session_date(ts)

    def session_phase(self, ts: datetime) -> SessionPhase | None:
        return session_phase(ts)

    def is_regular_hours(self, ts: datetime) -> bool:
        return is_regular_hours(ts)

    def regular_open_at(self, day: date) -> datetime:
        return datetime.combine(day, REGULAR_OPEN, tzinfo=US_EQUITY_TZ)


US_EQUITY_CALENDAR = UsEquityCalendar()

FX_ROLL = time(17, 0)
"""The FX day rolls at 17:00 New York, in New York's own DST - the
convention FX venues (IG included) settle and finance against."""

LONDON_TZ = ZoneInfo("Europe/London")
LONDON_OPEN = time(8, 0)


class FxCalendar(SessionCalendar):
    """Spot FX: Sunday 17:00 NY to Friday 17:00 NY, rolling daily at 17:00 NY.

    **A session is named by the New York date it ENDS on.** The session
    that opens Sunday 17:00 NY is Monday's; the one that opens Monday
    17:00 NY is Tuesday's. There are therefore exactly five sessions a
    week and no Saturday or Sunday session - the same "a bar belongs to the
    trading day it settles in" rule the equity calendar uses.

    Within a session, three parts, each an explicit assumption:

    * **Rollover blackout** (`rollover_blackout_minutes`, default 60) right
      after 17:00 NY. Liquidity is at its daily minimum while every venue
      rolls positions, and spreads routinely widen several-fold. Bars here
      belong to NO phase: they feed no level and are never traded.
    * **Pre-London** (the Asian session) until London's 08:00 open, in
      London's own DST. Classified as `PRE_MARKET` so the Asian range lands
      in `premarket_high`/`premarket_low` - the FX analogue of the equity
      pre-market range, and complete before anything trades against it.
    * **London + New York** from 08:00 London to the 17:00 NY roll, the
      `REGULAR` phase the intraday engine trades.

    `trade_asian_session=True` makes everything after the blackout
    `REGULAR` instead (no pre-market range at all), for measuring whether
    excluding Asia matters rather than assuming it.

    The weekend - Friday 17:00 NY to Sunday 17:00 NY - is no phase. There
    is no holiday table, exactly as for equities: a day with no bars
    yields no session.
    """

    name = "fx_24h"
    causal_opening_range = True

    def __init__(
        self, *, rollover_blackout_minutes: int = 60, trade_asian_session: bool = False
    ) -> None:
        if not 0 <= rollover_blackout_minutes < 600:
            raise ValueError(
                "rollover_blackout_minutes must be in [0, 600); "
                f"got {rollover_blackout_minutes}."
            )
        self.rollover_blackout_minutes = rollover_blackout_minutes
        self.trade_asian_session = trade_asian_session

    def session_date(self, ts: datetime) -> date:
        local = to_exchange_time(ts)
        return local.date() + timedelta(days=1) if local.time() >= FX_ROLL else local.date()

    def session_open_at(self, day: date) -> datetime:
        """17:00 NY on the calendar day before `day` - the roll that opens it."""
        return datetime.combine(day - timedelta(days=1), FX_ROLL, tzinfo=US_EQUITY_TZ)

    def is_weekend_closed(self, ts: datetime) -> bool:
        local = to_exchange_time(ts)
        wd, t = local.weekday(), local.time()
        return (wd == 4 and t >= FX_ROLL) or wd == 5 or (wd == 6 and t < FX_ROLL)

    def session_phase(self, ts: datetime) -> SessionPhase | None:
        if self.is_weekend_closed(ts):
            return None
        day = self.session_date(ts)
        opened = self.session_open_at(day)
        if ts < opened + timedelta(minutes=self.rollover_blackout_minutes):
            return None
        if self.trade_asian_session or ts >= datetime.combine(
            day, LONDON_OPEN, tzinfo=LONDON_TZ
        ):
            return SessionPhase.REGULAR
        return SessionPhase.PRE_MARKET

    def regular_open_at(self, day: date) -> datetime:
        if self.trade_asian_session:
            return self.session_open_at(day) + timedelta(
                minutes=self.rollover_blackout_minutes
            )
        return datetime.combine(day, LONDON_OPEN, tzinfo=LONDON_TZ)


FX_CALENDAR = FxCalendar()
"""The default FX clock: one-hour rollover blackout, Asia as pre-market."""

FX_24H_CALENDAR = FxCalendar(rollover_blackout_minutes=0, trade_asian_session=True)
"""Phase 106 (D130): the clock of an Autotrade bot set to trade 24 hours -
every hour from the Sunday 17:00 NY open to the Friday 17:00 NY close is
tradable, so the only forced flat is the weekend."""


class Crypto24hCalendar(SessionCalendar):
    """Phase 106 (D130): crypto never closes. A session is one UTC day and
    every bar in it is `REGULAR`, so the session functions (levels, VWAP,
    opening range) keep working and nothing is ever forced flat."""

    name = "crypto_24h"
    causal_opening_range = True

    def session_date(self, ts: datetime) -> date:
        return ts.astimezone(UTC).date()

    def session_phase(self, ts: datetime) -> SessionPhase | None:
        return SessionPhase.REGULAR

    def regular_open_at(self, day: date) -> datetime:
        return datetime.combine(day, time(0, 0), tzinfo=UTC)


CRYPTO_24H_CALENDAR = Crypto24hCalendar()


def _cal(calendar: SessionCalendar | None) -> SessionCalendar:
    return US_EQUITY_CALENDAR if calendar is None else calendar


@dataclass(frozen=True)
class SessionLevels:
    """The fixed locations a session's reversal setups are measured against.

    Every field is optional and `None` means "the data does not support
    this level", never zero and never a carried-forward value. The first
    session in any window legitimately has no previous day; a session whose
    premarket never traded has no premarket high. Downstream, `None` must
    read as "no level here, so no setup here" - which is why these are not
    defaulted.
    """

    session_date: date
    previous_high: Decimal | None = None
    previous_low: Decimal | None = None
    previous_close: Decimal | None = None
    premarket_high: Decimal | None = None
    premarket_low: Decimal | None = None
    opening_range_high: Decimal | None = None
    opening_range_low: Decimal | None = None
    regular_open: Decimal | None = None
    opening_range_complete_at: datetime | None = None
    """When the opening range became KNOWN (Phase 103, D123). Set only by a
    calendar with `causal_opening_range`; `None` means "not tracked", which
    is the equity calendar's historical behaviour."""

    @property
    def has_previous_day(self) -> bool:
        return self.previous_high is not None and self.previous_low is not None

    def known_at(self, ts: datetime) -> SessionLevels:
        """These levels as a decision at bar `ts` could know them.

        A bar that OPENS before the opening range has finished cannot know
        the range's extremes - handing them to it is look-ahead. Previous
        day and pre-market levels are complete before the regular session
        starts, so only the opening range needs hiding.
        """
        if self.opening_range_complete_at is None or ts >= self.opening_range_complete_at:
            return self
        return replace(self, opening_range_high=None, opening_range_low=None)




def _high(bar: OHLCVBar) -> Decimal:
    return bar.high if bar.high is not None else bar.close


def _low(bar: OHLCVBar) -> Decimal:
    return bar.low if bar.low is not None else bar.close


def group_by_session(
    bars: Iterable[OHLCVBar], *, calendar: SessionCalendar | None = None
) -> dict[date, list[OHLCVBar]]:
    """Bars bucketed by exchange-local session date, each bucket ordered.

    Sorting here rather than trusting the caller is the same discipline the
    vendor adapters apply: an out-of-order bucket would make "the first
    fifteen minutes" whichever bars happened to arrive first.
    """
    cal = _cal(calendar)
    grouped: dict[date, list[OHLCVBar]] = {}
    for bar in bars:
        grouped.setdefault(cal.session_date(bar.ts), []).append(bar)
    for bucket in grouped.values():
        bucket.sort(key=lambda b: b.ts)
    return grouped


def build_session_levels(
    bars: Sequence[OHLCVBar],
    *,
    opening_range_minutes: int = DEFAULT_OPENING_RANGE_MINUTES,
    calendar: SessionCalendar | None = None,
) -> dict[date, SessionLevels]:
    """One `SessionLevels` per session date present in `bars`.

    **Previous-day levels are regular-hours only.** PDH and PDL are the
    levels a trader marks on the chart, and those are the regular session's
    extremes; folding in after-hours prints would move them to a spike that
    traded a few thousand shares at a spread nobody could hit. Premarket
    extremes are a SEPARATE pair of fields precisely because they are a
    different kind of level, used differently by the playbook.
    """
    if opening_range_minutes <= 0:
        raise ValueError(
            f"opening_range_minutes must be positive; got {opening_range_minutes}."
        )

    cal = _cal(calendar)
    grouped = group_by_session(bars, calendar=cal)
    levels: dict[date, SessionLevels] = {}
    previous: tuple[Decimal, Decimal, Decimal] | None = None

    # Ordered so each session sees the one actually before it in the data,
    # which is what makes a Monday's "previous day" the preceding Friday
    # and a post-holiday session's the last day that traded.
    for day in sorted(grouped):
        session_bars = grouped[day]
        regular = [b for b in session_bars if cal.is_regular_hours(b.ts)]
        premarket = [
            b for b in session_bars if cal.session_phase(b.ts) is SessionPhase.PRE_MARKET
        ]

        opening_range_high = opening_range_low = None
        cutoff = cal.regular_open_at(day) + timedelta(minutes=opening_range_minutes)
        if regular:
            window = [b for b in regular if b.ts < cutoff]
            if window:
                opening_range_high = max(_high(b) for b in window)
                opening_range_low = min(_low(b) for b in window)

        levels[day] = SessionLevels(
            session_date=day,
            previous_high=previous[0] if previous else None,
            previous_low=previous[1] if previous else None,
            previous_close=previous[2] if previous else None,
            premarket_high=max((_high(b) for b in premarket), default=None),
            premarket_low=min((_low(b) for b in premarket), default=None),
            opening_range_high=opening_range_high,
            opening_range_low=opening_range_low,
            regular_open=regular[0].open if regular and regular[0].open else None,
            opening_range_complete_at=cutoff if cal.causal_opening_range else None,
        )

        # Only a session that actually traded regular hours becomes the
        # next one's "previous day". A data gap therefore carries the last
        # REAL session forward rather than blanking the level - the level
        # traders are watching does not disappear because a feed dropped.
        if regular:
            previous = (
                max(_high(b) for b in regular),
                min(_low(b) for b in regular),
                regular[-1].close,
            )

    return levels


@dataclass(frozen=True)
class VwapPoint:
    """Session VWAP and its volume-weighted deviation bands at one bar."""

    ts: datetime
    vwap: Decimal
    sigma: Decimal

    def band(self, multiple: Decimal | int) -> tuple[Decimal, Decimal]:
        """The ±k σ pair. Returned together because the playbook always
        reads them as a pair (`-2σ or -3σ`), and computing one without the
        other invites a sign error at the call site."""
        offset = self.sigma * Decimal(multiple)
        return (self.vwap - offset, self.vwap + offset)


def session_vwap(
    bars: Sequence[OHLCVBar],
    *,
    regular_hours_only: bool = True,
    calendar: SessionCalendar | None = None,
) -> list[VwapPoint]:
    """Cumulative session VWAP with volume-weighted σ, reset each session.

    **Anchored at the regular open by default.** "Session VWAP" on a US
    equity chart means the line that starts at 09:30; including premarket
    prints drags the anchor toward a few thousand thin shares and shifts
    every deviation band with it. `regular_hours_only=False` exists for
    instruments that genuinely trade one continuous session.

    σ is the volume-weighted standard deviation of the typical price about
    the running VWAP - `sqrt(Σv(p-vwap)² / Σv)` - so a band is a statement
    about where volume actually traded, not about bar-to-bar noise.

    Bars with no volume are skipped rather than counted as zero-weight
    ticks: a zero-volume bar contributes nothing to a volume-weighted mean,
    and treating its price as a data point would let an untraded print move
    a level that exists to describe traded value.
    """
    points: list[VwapPoint] = []
    cumulative_volume = Decimal(0)
    cumulative_pv = Decimal(0)
    cumulative_pv2 = Decimal(0)
    current_session: date | None = None

    cal = _cal(calendar)
    for bar in sorted(bars, key=lambda b: b.ts):
        if regular_hours_only and not cal.is_regular_hours(bar.ts):
            continue
        day = cal.session_date(bar.ts)
        if day != current_session:
            current_session = day
            cumulative_volume = Decimal(0)
            cumulative_pv = Decimal(0)
            cumulative_pv2 = Decimal(0)

        volume = Decimal(bar.volume) if bar.volume else Decimal(0)
        if volume <= 0:
            continue

        typical = (_high(bar) + _low(bar) + bar.close) / Decimal(3)
        cumulative_volume += volume
        cumulative_pv += typical * volume
        cumulative_pv2 += typical * typical * volume

        vwap = cumulative_pv / cumulative_volume
        # E[p²] - E[p]², floored at zero: the two cumulants are tracked
        # independently, so rounding can leave the difference fractionally
        # negative on a session where every trade printed at one price.
        variance = (cumulative_pv2 / cumulative_volume) - (vwap * vwap)
        sigma = variance.sqrt() if variance > 0 else Decimal(0)
        points.append(VwapPoint(ts=bar.ts, vwap=vwap, sigma=sigma))

    return points


@dataclass(frozen=True)
class PhaseMove:
    """What a single session phase actually did, from our own bars.

    `change`/`change_pct` are `None` when the phase has no reference price
    in the data. That is common and ordinary — a pre-market phase on the
    first session of a window has no previous close to be measured against
    — and reporting it as zero would say "unchanged", which is a claim
    about the market rather than about the data.
    """

    phase: SessionPhase
    bars: int
    first: Decimal
    last: Decimal
    high: Decimal
    low: Decimal
    volume: int | None
    """Summed across the phase, or None when any bar in it reported none."""
    reference: Decimal | None
    reference_label: str
    change: Decimal | None
    change_pct: Decimal | None


def _phase_move(
    phase: SessionPhase,
    bars: Sequence[OHLCVBar],
    reference: Decimal | None,
    reference_label: str,
) -> PhaseMove | None:
    if not bars:
        return None
    volumes = [b.volume for b in bars]
    total = None if any(v is None for v in volumes) else sum(v for v in volumes if v is not None)
    last = bars[-1].close
    change = change_pct = None
    if reference is not None and reference != 0:
        change = last - reference
        change_pct = (change / reference) * Decimal(100)
    return PhaseMove(
        phase=phase,
        bars=len(bars),
        first=bars[0].open if bars[0].open is not None else bars[0].close,
        last=last,
        high=max(_high(b) for b in bars),
        low=min(_low(b) for b in bars),
        volume=total,
        reference=reference,
        reference_label=reference_label,
        change=change,
        change_pct=change_pct,
    )


def extended_hours_moves(
    bars: Sequence[OHLCVBar],
) -> tuple[date | None, dict[SessionPhase, PhaseMove]]:
    """Pre-market, regular and after-hours movement for the latest stored
    session (Phase 86, D105).

    Each phase is measured against the price a trader would actually
    compare it to, and the comparison is named in the response rather than
    left implicit:

      * **pre-market** against the PREVIOUS session's regular close — the
        last price at which the market agreed on anything.
      * **regular** against that same previous close, which is what every
        quoted daily % change means.
      * **after-hours** against THIS session's regular close, because an
        after-hours move is by definition a move away from the close.

    A phase with no bars is absent from the mapping. It is not synthesised
    from the neighbouring phase: an empty pre-market means nothing traded
    before the open, and carrying the previous close into it would draw a
    flat pre-market that looks like a quiet market rather than no market.
    """
    grouped = group_by_session(bars)
    if not grouped:
        return None, {}
    days = sorted(grouped)
    today = days[-1]

    previous_close: Decimal | None = None
    if len(days) >= 2:
        prior_regular = [
            b for b in grouped[days[-2]] if session_phase(b.ts) is SessionPhase.REGULAR
        ]
        if prior_regular:
            previous_close = prior_regular[-1].close

    by_phase: dict[SessionPhase, list[OHLCVBar]] = {}
    for bar in grouped[today]:
        phase = session_phase(bar.ts)
        if phase is None:
            continue
        by_phase.setdefault(phase, []).append(bar)

    regular = by_phase.get(SessionPhase.REGULAR) or []
    regular_close = regular[-1].close if regular else None

    prev_label = (
        f"previous regular close ({days[-2].isoformat()})" if previous_close is not None
        else "no previous regular close in this window"
    )
    moves: dict[SessionPhase, PhaseMove] = {}
    for phase, reference, label in (
        (SessionPhase.PRE_MARKET, previous_close, prev_label),
        (SessionPhase.REGULAR, previous_close, prev_label),
        (
            SessionPhase.AFTER_HOURS,
            regular_close,
            "this session's regular close" if regular_close is not None
            else "no regular close stored for this session",
        ),
    ):
        move = _phase_move(phase, by_phase.get(phase) or [], reference, label)
        if move is not None:
            moves[phase] = move
    return today, moves
