"""Strategy Library routes (Phase 108, D135).

    GET   /library/strategies     the catalogue: rules, evidence, universes
    POST  /library/backtest       run one strategy on real daily history

Both need `strategy:backtest` - the same permission as any other backtest.
Neither writes a row or touches a broker: a library backtest is a
measurement over cached public history, recomputed on request.

A refusal carries a code in the detail (`DATA_UNAVAILABLE`,
`DATA_QUALITY`, `INSUFFICIENT_DATA`, `INVALID_REQUEST`) so the page can
say which kind of problem it is.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from apps.api.app.auth.dependencies import require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.config import get_settings
from apps.api.app.db.models import User
from apps.api.app.library.metrics import Metrics
from apps.api.app.library.service import (
    MAX_SYMBOLS,
    BacktestReport,
    DailyHistoryProvider,
    LibraryError,
    run_backtest,
)
from apps.api.app.library.strategies import (
    COST_BPS,
    PRESETS,
    STRATEGIES,
    LibraryStrategy,
    get_preset,
)
from apps.api.app.marketdata.providers.yahoo import YahooDailyProvider

router = APIRouter(prefix="/library", tags=["library"])

_PROVIDER: YahooDailyProvider | None = None


def get_library_provider() -> DailyHistoryProvider:
    """The daily-history vendor, or a 503 naming the switch that is off."""
    global _PROVIDER
    if get_settings().library_data_provider == "none":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "NOT_CONFIGURED: the Strategy Library's data source is switched off "
            "(LIBRARY_DATA_PROVIDER=none).",
        )
    if _PROVIDER is None:
        _PROVIDER = YahooDailyProvider()
    return _PROVIDER


class CitationResponse(BaseModel):
    authors: str
    year: int
    title: str
    venue: str
    url: str | None


class ParamResponse(BaseModel):
    key: str
    label: str
    default: float
    minimum: float
    maximum: float
    help: str
    integer: bool


class StrategyResponse(BaseModel):
    id: str
    name: str
    family: str
    cadence: str
    summary: str
    rules: list[str]
    evidence: list[CitationResponse]
    published_year: int
    params: list[ParamResponse]
    caveats: list[str]
    min_symbols: int


class PresetResponse(BaseModel):
    id: str
    label: str
    market: str
    symbols: list[str]
    benchmark: list[str]
    strategies: list[str]
    note: str
    survivorship: bool


class CatalogueResponse(BaseModel):
    strategies: list[StrategyResponse]
    presets: list[PresetResponse]
    cost_bps: dict[str, float]
    data_source: str
    max_symbols: int
    disclaimer: str


class MetricsResponse(BaseModel):
    start: date
    end: date
    days: int
    years: float
    total_return: float
    cagr: float | None
    volatility: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float
    calmar: float | None
    t_stat: float | None
    best_year: float | None
    worst_year: float | None
    positive_years: int
    total_years: int

    @classmethod
    def of(cls, m: Metrics | None) -> MetricsResponse | None:
        return None if m is None else cls(**m.as_dict())


class SegmentResponse(BaseModel):
    label: str
    strategy: MetricsResponse | None
    benchmark: MetricsResponse | None


class VerdictResponse(BaseModel):
    code: str
    headline: str
    detail: str


class CurvePoint(BaseModel):
    d: date
    s: float
    b: float | None


class YearRow(BaseModel):
    year: int
    strategy: float
    benchmark: float | None


class HoldingResponse(BaseModel):
    symbol: str
    weight: float
    note: str | None


class SymbolDataResponse(BaseModel):
    symbol: str
    vendor_symbol: str
    currency: str | None
    first: date
    last: date
    bars: int
    last_close: float
    cost_bps: float
    removed: list[date]
    truncated_before: date | None


class BacktestResponse(BaseModel):
    strategy_id: str
    symbols: list[str]
    benchmark_symbols: list[str]
    params: dict[str, Any]
    lag: int
    published_year: int
    segments: list[SegmentResponse]
    verdict: VerdictResponse
    curve: list[CurvePoint]
    years: list[YearRow]
    trades: int
    turnover_per_year: float | None
    cost_drag_per_year: float | None
    avg_exposure: float
    target: list[HoldingResponse]
    decided_on: date | None
    data: list[SymbolDataResponse]
    warnings: list[str]
    source: str
    as_of: date
    generated_at: datetime

    @classmethod
    def of(cls, r: BacktestReport) -> BacktestResponse:
        noted = set(r.target) | set(r.explain)
        ordered = [s for s in r.symbols if s in noted]
        return cls(
            strategy_id=r.strategy_id,
            symbols=list(r.symbols),
            benchmark_symbols=list(r.benchmark_symbols),
            params=r.params,
            lag=r.lag,
            published_year=r.published.year,
            segments=[
                SegmentResponse(
                    label=s.label,
                    strategy=MetricsResponse.of(s.strategy),
                    benchmark=MetricsResponse.of(s.benchmark),
                )
                for s in r.segments
            ],
            verdict=VerdictResponse(
                code=r.verdict.code, headline=r.verdict.headline, detail=r.verdict.detail
            ),
            curve=[CurvePoint(d=d, s=s, b=b) for d, s, b in r.curve],
            years=[YearRow(year=y, strategy=s, benchmark=b) for y, s, b in r.years],
            trades=r.trades,
            turnover_per_year=r.turnover_per_year,
            cost_drag_per_year=r.cost_drag_per_year,
            avg_exposure=r.avg_exposure,
            target=[
                HoldingResponse(symbol=s, weight=r.target.get(s, 0.0), note=r.explain.get(s))
                for s in ordered
            ],
            decided_on=r.decided_on,
            data=[
                SymbolDataResponse(
                    symbol=d.symbol,
                    vendor_symbol=d.vendor_symbol,
                    currency=d.currency,
                    first=d.first,
                    last=d.last,
                    bars=d.bars,
                    last_close=d.last_close,
                    cost_bps=d.cost_bps,
                    removed=list(d.removed),
                    truncated_before=d.truncated_before,
                )
                for d in r.data
            ],
            warnings=list(r.warnings),
            source=r.source,
            as_of=r.as_of,
            generated_at=r.generated_at,
        )


class BacktestRequest(BaseModel):
    strategy_id: str = Field(min_length=1, max_length=64)
    preset_id: str | None = Field(default=None, max_length=64)
    symbols: list[str] | None = Field(default=None, max_length=MAX_SYMBOLS)
    benchmark: list[str] | None = Field(default=None, max_length=MAX_SYMBOLS)
    params: dict[str, float] = Field(default_factory=dict)
    lag: int = Field(default=1, ge=0, le=5)
    start: date | None = None
    cost_bps: float | None = Field(default=None, ge=0, le=500)


DISCLAIMER = (
    "Research measurements on historical data, not advice and not a forecast. A rule "
    "being published does not mean it will keep working; most of these reduce risk "
    "rather than beat buy-and-hold."
)

_STATUS = {
    "DATA_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    "DATA_QUALITY": 422,
    "INSUFFICIENT_DATA": 422,
    "INVALID_REQUEST": 422,
    "INVALID_SYMBOL": 422,
    "UNKNOWN_STRATEGY": status.HTTP_404_NOT_FOUND,
}


def _strategy(s: LibraryStrategy) -> StrategyResponse:
    return StrategyResponse(
        id=s.id,
        name=s.name,
        family=s.family,
        cadence=s.cadence,
        summary=s.summary,
        rules=list(s.rules),
        evidence=[
            CitationResponse(
                authors=c.authors, year=c.year, title=c.title, venue=c.venue, url=c.url
            )
            for c in s.evidence
        ],
        published_year=s.published.year,
        params=[
            ParamResponse(
                key=p.key,
                label=p.label,
                default=p.default,
                minimum=p.minimum,
                maximum=p.maximum,
                help=p.help,
                integer=p.integer,
            )
            for p in s.params
        ],
        caveats=list(s.caveats),
        min_symbols=s.min_symbols,
    )


@router.get("/strategies", response_model=CatalogueResponse)
async def catalogue(
    _: User = Depends(require_permission(Permission.STRATEGY_BACKTEST)),
) -> CatalogueResponse:
    return CatalogueResponse(
        strategies=[_strategy(s) for s in STRATEGIES],
        presets=[
            PresetResponse(
                id=p.id,
                label=p.label,
                market=p.market,
                symbols=list(p.symbols),
                benchmark=list(p.benchmark),
                strategies=list(p.strategies),
                note=p.note,
                survivorship=p.survivorship,
            )
            for p in PRESETS
        ],
        cost_bps=dict(COST_BPS),
        data_source=get_settings().library_data_provider,
        max_symbols=MAX_SYMBOLS,
        disclaimer=DISCLAIMER,
    )


@router.post("/backtest", response_model=BacktestResponse)
async def backtest(
    request: BacktestRequest,
    _: User = Depends(require_permission(Permission.STRATEGY_BACKTEST)),
    provider: DailyHistoryProvider = Depends(get_library_provider),
) -> BacktestResponse:
    preset = None
    symbols = request.symbols
    benchmark = request.benchmark
    if request.preset_id:
        try:
            preset = get_preset(request.preset_id)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc.args[0])) from None
        if symbols:
            # A hand-edited list is no longer the preset; keep only its notes
            # when the symbols are exactly the preset's own.
            if [s.strip().upper() for s in symbols] != list(preset.symbols):
                preset = None
        else:
            symbols = list(preset.symbols)
        if benchmark is None and preset is not None:
            benchmark = list(preset.benchmark)
    if not symbols:
        raise HTTPException(
            422,
            "INVALID_REQUEST: give a universe or at least one symbol.",
        )
    try:
        report = await run_backtest(
            provider,
            strategy_id=request.strategy_id,
            symbols=symbols,
            benchmark=benchmark,
            params=dict(request.params),
            lag=request.lag,
            start=request.start,
            cost_bps=request.cost_bps,
            preset=preset,
        )
    except LibraryError as exc:
        raise HTTPException(_STATUS.get(exc.code, 422), str(exc)) from None
    return BacktestResponse.of(report)
