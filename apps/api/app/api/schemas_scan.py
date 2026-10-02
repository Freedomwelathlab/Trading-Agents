"""Wire shapes of the bot scan dashboards (Phase 107, D133)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from apps.api.app.bots.scanboard import SetupScan, SymbolBoard


class SetupScanResponse(BaseModel):
    setup: str
    enabled: bool
    fired: bool
    direction: str | None
    score: int | None
    entry: Decimal | None
    stop: Decimal | None
    target_1r: Decimal | None
    risk_pct: Decimal | None
    confidence_pct: Decimal | None
    sample: int | None
    verdict: str

    @classmethod
    def of(cls, s: SetupScan) -> SetupScanResponse:
        return cls(
            setup=s.setup, enabled=s.enabled, fired=s.fired, direction=s.direction,
            score=s.score, entry=s.entry, stop=s.stop, target_1r=s.target_1r,
            risk_pct=s.risk_pct, confidence_pct=s.confidence_pct, sample=s.sample,
            verdict=s.verdict,
        )


class SymbolBoardResponse(BaseModel):
    symbol: str
    asset_class: str
    last_close: Decimal | None
    last_ts: datetime | None
    phase: str | None
    recommendation: str
    headline: str
    best: SetupScanResponse | None
    setups: list[SetupScanResponse]
    bars_used: int
    calibration: str = "ready"
    """ready | calculating | unavailable: ... - the measured confidence
    replay runs in the background once a day (D133)."""

    @classmethod
    def of(cls, b: SymbolBoard, *, calibration: str = "ready") -> SymbolBoardResponse:
        return cls(
            calibration=calibration,
            symbol=b.symbol, asset_class=b.asset_class, last_close=b.last_close,
            last_ts=b.last_ts, phase=b.phase, recommendation=b.recommendation,
            headline=b.headline, best=SetupScanResponse.of(b.best) if b.best else None,
            setups=[SetupScanResponse.of(s) for s in b.setups], bars_used=b.bars_used,
        )


class BotScanResponse(BaseModel):
    bot_id: str
    name: str
    asset_class: str
    market_type: str
    bar_interval: str
    min_score: int
    directions: list[str]
    generated_at: datetime
    boards: list[SymbolBoardResponse]
    evidence: list[str]
    """What the platform's own research measured for this asset class -
    shown beside the recommendation so a score is never read as an edge
    the research did not find."""
