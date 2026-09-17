"""Universal option-selection filters (Phase 75, D093).

The playbook's §2 gates every option trade behind liquidity, DTE, delta and
IV rules before any strategy is allowed to act. These are pure predicates
so they can run identically in a backtest and against a live chain, and so
each rejection carries a reason rather than a silent skip.

Unlike pricing.py, these operate on REAL quote inputs when live (bid/ask,
open interest, IV rank). In a model-priced backtest the liquidity gate is
not meaningful — there is no real spread — so the backtest layer records
that it was not applied rather than pretending a modeled contract passed a
liquidity check it never faced.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal


class DteBand(str, enum.Enum):  # noqa: UP042
    ZERO_DTE = "0dte"          # highest-quality setups only, tight risk
    TACTICAL = "tactical"       # 1-3 DTE
    REGULAR = "regular"         # 3-14 DTE, the default directional band
    TOO_LONG = "too_long"       # > 14 DTE, outside the intraday playbook


def classify_dte(dte_days: int) -> DteBand:
    if dte_days <= 0:
        return DteBand.ZERO_DTE
    if dte_days <= 3:
        return DteBand.TACTICAL
    if dte_days <= 14:
        return DteBand.REGULAR
    return DteBand.TOO_LONG


@dataclass(frozen=True)
class LiquidityCheck:
    passed: bool
    reason: str
    spread_pct: Decimal | None


def check_liquidity(
    *,
    bid: Decimal,
    ask: Decimal,
    open_interest: int,
    is_multi_leg: bool,
    min_open_interest: int = 100,
) -> LiquidityCheck:
    """Reject a contract on the playbook's §2 liquidity rules.

    Spread as a fraction of mid must be within 10% for a single leg, 5% for
    a multi-leg structure. A non-positive or crossed market, or a mid of
    zero, is rejected outright — those are the "stale / abnormally thin"
    cases the playbook names, and computing a percentage from them would
    manufacture a pass.
    """
    if bid <= 0 or ask <= 0 or ask < bid:
        return LiquidityCheck(False, "no two-sided market (stale or crossed quote)", None)
    mid = (bid + ask) / 2
    if mid <= 0:
        return LiquidityCheck(False, "mid price is zero", None)
    if open_interest < min_open_interest:
        return LiquidityCheck(
            False, f"open interest {open_interest} < {min_open_interest}", None
        )
    spread_pct = (ask - bid) / mid
    cap = Decimal("0.05") if is_multi_leg else Decimal("0.10")
    if spread_pct > cap:
        return LiquidityCheck(
            False, f"spread {spread_pct:.1%} exceeds {cap:.0%} cap", spread_pct
        )
    return LiquidityCheck(True, "ok", spread_pct)


def delta_in_band(delta_magnitude: float, low: float, high: float) -> bool:
    """The playbook's delta bands are magnitudes (a 0.30 put and a 0.30 call
    are both "0.30"), so callers pass |delta|."""
    return low <= abs(delta_magnitude) <= high


# The playbook's named bands, so callers reference one definition.
LONG_PREMIUM_DELTA = (0.45, 0.65)
STRONG_DIRECTIONAL_DELTA = (0.55, 0.70)
VERTICAL_LONG_LEG_DELTA = (0.50, 0.65)
VERTICAL_SHORT_LEG_DELTA = (0.20, 0.35)


def iv_regime_appropriate(*, iv_rank: float, is_debit: bool) -> bool:
    """§2 IV filter: buy debit premium when IV is normal-to-low, sell credit
    premium when IV is elevated. `iv_rank` is 0-1 (percentile of recent
    distribution). The thresholds are the playbook's starting research
    values and are meant to be validated, not trusted."""
    if not 0.0 <= iv_rank <= 1.0:
        raise ValueError("iv_rank must be between 0 and 1.")
    if is_debit:
        return iv_rank <= 0.50
    return iv_rank >= 0.50
