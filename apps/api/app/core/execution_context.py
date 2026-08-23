"""Typed execution contexts so a research/backtest code path cannot hold a
live credential by accident - the class itself is the guarantee, not a
runtime flag check scattered across call sites.

Per ARCHITECTURE-DISCOVERY-REPORT.md Part III: LiveContext must be
structurally impossible to construct except through build_execution_context,
which re-checks Settings' fail-closed live gate even though Settings already
enforces it at its own construction (spec Sec46 wants defense in depth, not a
single choke point).
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from apps.api.app.core.config import Settings, TradingMode


class ResearchContext(BaseModel):
    mode: Literal[TradingMode.RESEARCH] = TradingMode.RESEARCH


class PaperContext(BaseModel):
    mode: Literal[TradingMode.PAPER] = TradingMode.PAPER
    broker_id: UUID


class LiveContext(BaseModel):
    mode: Literal[TradingMode.LIVE] = TradingMode.LIVE
    broker_id: UUID


ExecutionContext = ResearchContext | PaperContext | LiveContext


def build_execution_context(
    settings: Settings, *, broker_id: UUID | None = None
) -> ExecutionContext:
    if settings.trading_mode is TradingMode.RESEARCH:
        return ResearchContext()

    if broker_id is None:
        raise ValueError(f"trading_mode={settings.trading_mode.value} requires a broker_id.")

    if settings.trading_mode is TradingMode.PAPER:
        return PaperContext(broker_id=broker_id)

    if not settings.live_trading_enabled:
        raise ValueError(
            "Refusing to build a LiveContext: live_trading_enabled is false. "
            "This should be unreachable because Settings already enforces this "
            "at construction - if you hit this, something bypassed Settings."
        )
    return LiveContext(broker_id=broker_id)
