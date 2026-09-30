"""Optimised setup profiles for the Autotrade bot's `tuned` strategy mode
(Phase 105, D126).

The walk-forward optimiser (`scripts/optimise_setups.py`, D118) finds, per
setup and symbol, the entry filter and stop distance that did best on the
most recent window, and says whether that setup held up OUT OF SAMPLE. This
module is how a PAPER bot uses that result.

**What a profile carries into the bot:** the minimum score, the allowed
directions and the ATR stop buffer (D125) - the parts the bot's scanner
applies. The optimiser's take-profit and trail levels are R-multiples of the
backtest engine's bracket; the bot manages exits with its own percentage
rules, so those levels are recorded for reference but NOT silently
translated into something they are not.

**Which setups trade:** only those flagged `promising` (positive out of
sample AND better than defaults) or `recommended` (also t >= 2 on >= 30
unseen trades). A setup that lost out of sample is never deployed, however
good its in-sample fit.

**Where profiles live:** JSON files in `profiles/`, one per symbol and bar
interval, committed with the code. Publishing new parameters is therefore a
reviewed commit and a deploy - never a silent change to a running bot - and
the improvement loop is: re-run the optimiser as bars accrue, publish,
review the diff, deploy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from apps.api.app.marketdata.structure import Direction

PROFILE_DIR = Path(__file__).parent / "profiles"

_DIRECTIONS: dict[str, tuple[Direction, ...]] = {
    "both": (Direction.LONG, Direction.SHORT),
    "long": (Direction.LONG,),
    "short": (Direction.SHORT,),
}


@dataclass(frozen=True)
class SetupProfile:
    setup: str
    min_score: int
    directions: tuple[Direction, ...]
    atr_stop_buffer: Decimal
    oos_trades: int
    oos_expectancy_r: float | None
    oos_t_stat: float | None
    promising: bool
    recommended: bool

    @property
    def deployable(self) -> bool:
        return self.promising or self.recommended


def profile_path(symbol: str, bar_interval: str) -> Path:
    return PROFILE_DIR / f"{symbol.replace('.', '_')}_{bar_interval}.json"


@lru_cache(maxsize=64)
def load_profiles(symbol: str, bar_interval: str) -> dict[str, SetupProfile]:
    """Every setup's profile for this symbol/interval, or {} when none has
    been published - which the bot treats as "nothing deployable here"."""
    path = profile_path(symbol, bar_interval)
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, SetupProfile] = {}
    for name, p in doc.get("setups", {}).items():
        params = p["params"]
        out[name] = SetupProfile(
            setup=name,
            min_score=int(params["min_score"]),
            directions=_DIRECTIONS[params["directions"]],
            atr_stop_buffer=Decimal(str(params["atr_stop_buffer"])),
            oos_trades=int(p["out_of_sample"]["trades"]),
            oos_expectancy_r=p["out_of_sample"]["expectancy_r"],
            oos_t_stat=p["out_of_sample"]["t_stat"],
            promising=bool(p["promising"]),
            recommended=bool(p["recommended"]),
        )
    return out


def deployable_profiles(symbol: str, bar_interval: str) -> list[SetupProfile]:
    """Promising or recommended setups only, best out-of-sample first."""
    profiles = [p for p in load_profiles(symbol, bar_interval).values() if p.deployable]
    return sorted(profiles, key=lambda p: -(p.oos_expectancy_r or 0.0))
