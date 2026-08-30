"""Market-hours gating for the portfolio snapshot scheduler (Phase 35,
docs/DECISIONS.md D042), closing the gap D030's "Consequences" section
recorded: "the interval is wall-clock, not market-hours-aware: a scheduler
left enabled overnight will keep recording unchanged after-hours rows, or
keep logging skips".

WHAT THIS MODULE DELIBERATELY IS - AND IS NOT
---------------------------------------------
It is a **weekend gate**, not an exchange calendar. It answers exactly one
question, from arithmetic alone: "is the `as_of` instant a Saturday or a
Sunday in UTC?" If yes, the scheduler skips the cycle without touching the
database or the market-data vendor at all.

It is NOT a trading-day calendar and does not pretend to be one. It knows
nothing about public holidays, half-days, exchange-specific open/close
times, DST shifts, or which venue a given symbol trades on. Every one of
those requires real calendar data, and docs/TRADING_SAFETY.md / spec Sec57
forbid this codebase from inventing data it does not have. A hardcoded
"US equities are open 09:30-16:00 ET, closed on these 11 holidays" table
would be exactly that invention: it would look authoritative, it would
silently rot the first time an exchange changed a session or a government
moved a holiday, and a wrong "market is open" is indistinguishable
downstream from a real one. So it is not written. See D042's "Alternatives"
for what a real per-exchange gate would require and why it is explicitly
deferred rather than approximated.

WHY WEEKENDS ARE SAFE TO ASSERT WITHOUT VENDOR DATA
---------------------------------------------------
Saturday and Sunday are derived from the Gregorian calendar itself, not
from any exchange's policy. No configured venue in this repo's supported
set (Longbridge's US / HK / CN / SG equity markets) holds a regular equity
session on a Saturday or Sunday. That makes the weekend check a genuinely
universal, purely computable fact rather than a guess - which is precisely
why it is the one part of "market hours" that can be implemented honestly
with no new data source and no new dependency.

WHY UTC, AND THE ONE EDGE IT KNOWINGLY CLIPS
--------------------------------------------
The gate evaluates the day-of-week in UTC because UTC is the only clock
this process can read without a timezone database (Python's `zoneinfo`
needs the system tz data, which is absent on some deployment targets, and
adding `tzdata` is a new pip dependency this phase did not take - see
D042).

That choice is checked against, not assumed for, the real sessions:

  * Earliest regular open across the supported markets is 09:30 in
    Hong Kong / mainland China (UTC+8) = 01:30 UTC, which falls on a UTC
    *Monday*. The gate therefore never suppresses a Monday open.
  * Latest regular close is 16:00 US Eastern on Friday = 20:00 or 21:00
    UTC depending on DST, still a UTC *Friday*. The gate never suppresses
    a Friday regular session.

The single knowingly-clipped window is US *extended-hours* trading late on
a Friday: post-market to 20:00 ET is 00:00-01:00 UTC Saturday, which this
gate skips. That is accepted, not overlooked. A snapshot is a valuation of
held positions from last-done prices, one missed low-liquidity
extended-hours cycle leaves a gap in an append-only series rather than a
wrong row, and the whole gate can be turned off
(`PORTFOLIO_SNAPSHOT_MARKET_HOURS_GATE_ENABLED=false`) by anyone who wants
every cycle to fire regardless.

PURITY
------
`MarketHoursGate.evaluate()` performs no I/O of any kind: no vendor call,
no database read, no clock read. The caller supplies the instant. That
makes weekend behavior fully testable without waiting for an actual
weekend, and makes the decision deterministic given a real input.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

WEEKEND_WEEKDAYS = frozenset({5, 6})
"""`datetime.weekday()` values for Saturday (5) and Sunday (6). Named
rather than inlined so the one assumption in this module is greppable."""


class MarketHoursDecision(str, Enum):  # noqa: UP042 (str mixin for log/JSON interop)
    """Why a cycle was allowed to proceed, or was not. Typed and exhaustive
    for the same reason `ScheduledSnapshotStatus` is: "no snapshot exists
    for Saturday 14:00" must be distinguishable in the logs from "the
    vendor was down at Saturday 14:00"."""

    RUN = "run"
    """The gate is enabled and `as_of` is a weekday in UTC, so the cycle
    proceeds normally."""

    RUN_GATE_DISABLED = "run_gate_disabled"
    """The gate is switched off, so the cycle proceeds without any
    market-hours consideration at all - the exact pre-Phase-35 (D030)
    behavior. Reported distinctly from RUN so a log reader can tell "it was
    a weekday" from "nobody was checking"."""

    SKIP_WEEKEND = "skip_weekend"
    """`as_of` is a Saturday or Sunday in UTC. The whole cycle is skipped
    before any broker is enumerated, so it costs zero database queries and
    zero vendor calls."""

    @property
    def should_run(self) -> bool:
        return self is not MarketHoursDecision.SKIP_WEEKEND


@dataclass(frozen=True)
class MarketHoursGate:
    """The scheduler's market-hours policy. Frozen and I/O-free; see the
    module docstring for the deliberate limits of what it claims to know."""

    enabled: bool = True
    """Default TRUE: once a market-hours check exists at all, skipping
    outside it is the cheaper and more honest behavior (fewer wasted vendor
    calls, fewer unchanged after-hours rows), so that is what an operator
    who enables the scheduler and thinks no further about it gets. Set
    false to restore D030's unconditional wall-clock firing - useful for
    testing, for non-equity/24-7 instruments, or for anyone who genuinely
    wants a row every interval."""

    def evaluate(self, as_of: datetime) -> MarketHoursDecision:
        """Decides whether a cycle starting at `as_of` should run.

        `as_of` must be timezone-aware. A naive datetime is rejected rather
        than assumed to be UTC: silently guessing an offset is the same
        class of error as guessing a price, and here it would be a guess
        that can flip a Friday into a Saturday.
        """
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError(
                "MarketHoursGate.evaluate() requires a timezone-aware datetime; a naive "
                "one would have to be assumed to be in some timezone, and that assumption "
                "can silently move the instant across a day boundary and so across the "
                "weekend check itself."
            )

        if not self.enabled:
            return MarketHoursDecision.RUN_GATE_DISABLED

        if as_of.astimezone(UTC).weekday() in WEEKEND_WEEKDAYS:
            return MarketHoursDecision.SKIP_WEEKEND

        return MarketHoursDecision.RUN


def utc_now() -> datetime:
    """The scheduler's default clock, injected rather than called inline so
    tests can drive a real weekend/weekday transition without waiting for
    one."""
    return datetime.now(UTC)
