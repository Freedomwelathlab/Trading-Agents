"""The library's strategies, their evidence, and the universes they run on.

Each strategy is a rule somebody PUBLISHED, with the year it became public.
That year matters more than the rule: results before it are what the
author saw when choosing the rule, results after it are the only ones that
were not available to fit to. The backtest reports the two separately.

Rules are implemented as written, on closes, long-or-cash unless a
parameter says otherwise. No parameter here was tuned on this platform's
data - the defaults are the published ones.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from apps.api.app.library.engine import PriceTable, StrategyFn, Weights
from apps.api.app.marketdata.fx import is_fx_symbol


@dataclass(frozen=True)
class Citation:
    authors: str
    year: int
    title: str
    venue: str
    url: str | None = None


@dataclass(frozen=True)
class ParamSpec:
    key: str
    label: str
    default: float
    minimum: float
    maximum: float
    help: str
    integer: bool = True


@dataclass(frozen=True)
class LibraryStrategy:
    id: str
    name: str
    family: str
    cadence: str
    summary: str
    rules: tuple[str, ...]
    evidence: tuple[Citation, ...]
    published: date
    params: tuple[ParamSpec, ...]
    fn: StrategyFn
    warmup: Callable[[dict[str, Any]], int]
    min_ready: Callable[[dict[str, Any], int], int]
    caveats: tuple[str, ...] = ()
    min_symbols: int = 1

    def resolve_params(self, supplied: dict[str, Any] | None) -> dict[str, Any]:
        """Defaults overlaid with the caller's values, each range-checked."""
        out: dict[str, Any] = {}
        supplied = supplied or {}
        known = {p.key for p in self.params}
        unknown = sorted(set(supplied) - known)
        if unknown:
            raise ValueError(f"Unknown parameter(s) for {self.id}: {', '.join(unknown)}.")
        for spec in self.params:
            raw = supplied.get(spec.key, spec.default)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{spec.key} must be a number.") from None
            if not spec.minimum <= value <= spec.maximum:
                raise ValueError(
                    f"{spec.key} must be between {spec.minimum:g} and {spec.maximum:g}."
                )
            out[spec.key] = int(value) if spec.integer else value
        return out


def _note(state: dict[str, Any], symbol: str, text: str) -> None:
    state.setdefault("explain", {})[symbol] = text


def _ready(table: PriceTable, i: int, bars: int) -> list[str]:
    return [s for s in table.symbols if table.bars(s, i) > bars]


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------


def equal_weight(
    table: PriceTable, i: int, p: dict[str, Any], state: dict[str, Any]
) -> Weights | None:
    """Hold everything that has listed, equally, rebalanced monthly."""
    listed = [s for s in table.symbols if table.price(s, i) is not None]
    key = tuple(listed)
    if state.get("members") == key and not table.is_month_end(i):
        return None
    state["members"] = key
    if not listed:
        return {}
    return {s: 1.0 / len(listed) for s in listed}


# --------------------------------------------------------------------------
# Trend following
# --------------------------------------------------------------------------


def trend_filter(
    table: PriceTable, i: int, p: dict[str, Any], state: dict[str, Any]
) -> Weights | None:
    if not table.is_month_end(i):
        return None
    period = p["sma_days"]
    ready = _ready(table, i, period)
    if not ready:
        return {}
    sleeve = 1.0 / len(ready)
    out: Weights = {}
    for s in ready:
        px, avg = table.price(s, i), table.sma(s, i, period)
        if px is None or avg is None:
            continue
        above = px > avg
        _note(
            state,
            s,
            f"close {px:.4g} is {'above' if above else 'below'} its {period}-day average {avg:.4g}",
        )
        if above:
            out[s] = sleeve
    return out


def time_series_momentum(
    table: PriceTable, i: int, p: dict[str, Any], state: dict[str, Any]
) -> Weights | None:
    if not table.is_month_end(i):
        return None
    lookback = p["lookback_days"]
    ready = _ready(table, i, lookback)
    if not ready:
        return {}
    sleeve = 1.0 / len(ready)
    out: Weights = {}
    for s in ready:
        r = table.ret(s, i, lookback)
        if r is None:
            continue
        scale = 1.0
        if p["target_vol_pct"] > 0:
            vol = table.realised_vol(s, i, 60)
            if vol and vol > 0:
                scale = min(1.0, (p["target_vol_pct"] / 100.0) / vol)
        direction = 1.0 if r > 0 else (-1.0 if p["allow_short"] else 0.0)
        _note(
            state,
            s,
            f"{lookback}-day return {r * 100:+.1f}%, sized at {scale * 100:.0f}% of its sleeve",
        )
        if direction:
            out[s] = direction * sleeve * scale
    return out


def donchian_breakout(
    table: PriceTable, i: int, p: dict[str, Any], state: dict[str, Any]
) -> Weights | None:
    entry, exit_ = p["entry_days"], p["exit_days"]
    held: set[str] = state.setdefault("held", set())
    changed = False
    for s in _ready(table, i, entry):
        px = table.price(s, i)
        if px is None:
            continue
        if s in held:
            floor = table.lowest(s, i, exit_)
            if floor is not None and px < floor:
                held.discard(s)
                changed = True
                _note(state, s, f"close {px:.4g} broke the {exit_}-day low {floor:.4g}: exit")
            elif floor is not None:
                _note(state, s, f"holding; exit below the {exit_}-day low {floor:.4g}")
        else:
            ceiling = table.highest(s, i, entry)
            if ceiling is not None and px > ceiling:
                held.add(s)
                changed = True
                _note(state, s, f"close {px:.4g} made a {entry}-day high: enter")
            elif ceiling is not None:
                _note(state, s, f"flat; enters on a close above {ceiling:.4g}")
    if not changed:
        return None
    sleeve = 1.0 / len(table.symbols)
    return {s: sleeve for s in held}


# --------------------------------------------------------------------------
# Momentum rotation
# --------------------------------------------------------------------------


def dual_momentum(
    table: PriceTable, i: int, p: dict[str, Any], state: dict[str, Any]
) -> Weights | None:
    """Every symbol but the last competes; the last is the shelter."""
    if not table.is_month_end(i):
        return None
    lookback = p["lookback_days"]
    *risk, shelter = table.symbols
    scored: list[tuple[float, str]] = []
    for s in risk:
        r = table.ret(s, i, lookback) if table.bars(s, i) > lookback else None
        if r is not None:
            scored.append((r, s))
    if not scored:
        return {}
    best_return, best = max(scored)
    for r, s in scored:
        _note(state, s, f"{lookback}-day return {r * 100:+.1f}%")
    if best_return > 0:
        _note(
            state,
            best,
            f"{lookback}-day return {best_return * 100:+.1f}% - the strongest, and positive: hold",
        )
        return {best: 1.0}
    if table.price(shelter, i) is None:
        return {}
    _note(state, shelter, "no risk asset has a positive return: shelter here")
    return {shelter: 1.0}


def momentum_rotation(
    table: PriceTable, i: int, p: dict[str, Any], state: dict[str, Any]
) -> Weights | None:
    if not table.is_month_end(i):
        return None
    lookback, skip, top = p["lookback_days"], p["skip_days"], p["top_n"]
    scored: list[tuple[float, str]] = []
    for s in _ready(table, i, lookback):
        then, recent = table.price(s, i - lookback), table.price(s, i - skip)
        if then and recent:
            scored.append((recent / then - 1.0, s))
    if not scored:
        return {}
    scored.sort(reverse=True)
    out: Weights = {}
    for rank, (momentum, s) in enumerate(scored, start=1):
        picked = rank <= top and (momentum > 0 or not p["require_positive"])
        _note(
            state,
            s,
            f"rank {rank} of {len(scored)}, momentum {momentum * 100:+.1f}%"
            + (": hold" if picked else ""),
        )
        if picked:
            out[s] = 1.0 / top
    return out


# --------------------------------------------------------------------------
# Short-term reversal
# --------------------------------------------------------------------------


def rsi2_reversion(
    table: PriceTable, i: int, p: dict[str, Any], state: dict[str, Any]
) -> Weights | None:
    trend, exit_, threshold = p["trend_days"], p["exit_sma_days"], p["entry_rsi"]
    held: set[str] = state.setdefault("held", set())
    changed = False
    for s in _ready(table, i, trend):
        px = table.price(s, i)
        if px is None:
            continue
        if s in held:
            avg = table.sma(s, i, exit_)
            if avg is not None and px > avg:
                held.discard(s)
                changed = True
                _note(state, s, f"close {px:.4g} back above its {exit_}-day average: exit")
            else:
                _note(state, s, f"holding until a close above the {exit_}-day average")
        else:
            long_avg, rsi = table.sma(s, i, trend), table.rsi(s, i, 2)
            if long_avg is None or rsi is None:
                continue
            if px > long_avg and rsi < threshold:
                held.add(s)
                changed = True
                _note(state, s, f"uptrend and RSI(2) {rsi:.0f} under {threshold}: enter")
            else:
                side = "uptrend" if px > long_avg else "below its long average"
                _note(state, s, f"flat; {side}, RSI(2) {rsi:.0f}")
    if not changed:
        return None
    sleeve = 1.0 / len(table.symbols)
    return {s: sleeve for s in held}


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

_FABER = Citation(
    "Faber, M.",
    2007,
    "A Quantitative Approach to Tactical Asset Allocation",
    "Journal of Wealth Management",
    "https://papers.ssrn.com/abstract=962461",
)
_MOP = Citation(
    "Moskowitz, T., Ooi, Y. H., Pedersen, L. H.",
    2012,
    "Time Series Momentum",
    "Journal of Financial Economics 104(2)",
    "https://papers.ssrn.com/abstract=2089463",
)
_HOP = Citation(
    "Hurst, B., Ooi, Y. H., Pedersen, L. H.",
    2017,
    "A Century of Evidence on Trend-Following Investing",
    "Journal of Portfolio Management 44(1)",
)
_ANTONACCI = Citation(
    "Antonacci, G.",
    2013,
    "Risk Premia Harvesting Through Dual Momentum",
    "Journal of Management & Entrepreneurship",
    "https://papers.ssrn.com/abstract=2042750",
)
_JT = Citation(
    "Jegadeesh, N., Titman, S.",
    1993,
    "Returns to Buying Winners and Selling Losers: Implications for Stock Market Efficiency",
    "Journal of Finance 48(1)",
)
_CTW = Citation(
    "Chui, A., Titman, S., Wei, K. C. J.",
    2010,
    "Individualism and Momentum around the World",
    "Journal of Finance 65(1)",
)
_CONNORS = Citation(
    "Connors, L., Alvarez, C.",
    2008,
    "Short Term Trading Strategies That Work",
    "TradingMarkets (practitioner book)",
)
_LEHMANN = Citation(
    "Lehmann, B.",
    1990,
    "Fads, Martingales, and Market Efficiency",
    "Quarterly Journal of Economics 105(1)",
)
_FAITH = Citation(
    "Faith, C.",
    2007,
    "Way of the Turtle",
    "McGraw-Hill (the 1983 Turtle rules, published)",
)
_LIU = Citation(
    "Liu, Y., Tsyvinski, A.",
    2021,
    "Risks and Returns of Cryptocurrency",
    "Review of Financial Studies 34(6)",
)

STRATEGIES: tuple[LibraryStrategy, ...] = (
    LibraryStrategy(
        id="trend_200d",
        name="200-day trend filter",
        family="Trend following",
        cadence="monthly",
        summary=(
            "Hold an asset only while it closes above its 200-day average; otherwise "
            "hold cash. Checked once a month."
        ),
        rules=(
            "On the last session of each month, compare each asset's close with its "
            "200-day average.",
            "Above: hold its equal share of the portfolio. Below: hold that share in cash.",
            "Trade on the next session's close.",
        ),
        evidence=(_FABER, _HOP),
        published=date(2007, 2, 1),
        params=(
            ParamSpec(
                "sma_days",
                "Average length (sessions)",
                200,
                50,
                400,
                "Faber used a 10-month average, about 200 sessions.",
            ),
        ),
        fn=trend_filter,
        warmup=lambda p: p["sma_days"],
        min_ready=lambda p, n: 1,
        caveats=(
            "Its documented benefit is smaller drawdowns, not higher returns: in a long "
            "bull market it lags buy-and-hold.",
            "Whipsaws in sideways markets cost money every time the price crosses the average.",
        ),
    ),
    LibraryStrategy(
        id="tsmom_12m",
        name="12-month time-series momentum",
        family="Trend following",
        cadence="monthly",
        summary=(
            "Hold an asset while its own trailing 12-month return is positive, sized down "
            "when it is volatile."
        ),
        rules=(
            "On the last session of each month, measure each asset's 252-session return.",
            "Positive: hold its share, scaled down so the sleeve targets the chosen volatility "
            "(never scaled above 100%). Negative: cash, or short if shorting is switched on.",
            "Trade on the next session's close.",
        ),
        evidence=(_MOP, _HOP, _LIU),
        published=date(2012, 5, 1),
        params=(
            ParamSpec(
                "lookback_days", "Lookback (sessions)", 252, 60, 504, "The paper's 12 months."
            ),
            ParamSpec(
                "target_vol_pct",
                "Target volatility % (0 = off)",
                15,
                0,
                60,
                "Each sleeve is scaled to this annual volatility, capped at fully invested.",
                integer=False,
            ),
            ParamSpec(
                "allow_short",
                "Short when negative (1 = yes)",
                0,
                0,
                1,
                "The paper is long/short on futures. Shorting cash assets has borrow and "
                "financing costs this backtest does not charge.",
            ),
        ),
        fn=time_series_momentum,
        warmup=lambda p: max(p["lookback_days"], 61),
        min_ready=lambda p, n: 1,
        caveats=(
            "The paper tested 58 futures markets together. One asset alone is a far noisier bet.",
            "Trend-following returns were weak for most of 2009-2019.",
        ),
    ),
    LibraryStrategy(
        id="donchian_breakout",
        name="Donchian breakout (Turtle rules)",
        family="Trend following",
        cadence="daily",
        summary=(
            "Buy a close above the 55-day high; sell a close below the 20-day low. Long or cash."
        ),
        rules=(
            "Enter when the close is above the highest close of the previous 55 sessions.",
            "Exit when the close is below the lowest close of the previous 20 sessions.",
            "Each asset has an equal fixed share; trade on the next session's close.",
        ),
        evidence=(_FAITH, _HOP),
        published=date(2007, 3, 1),
        params=(
            ParamSpec("entry_days", "Entry channel (sessions)", 55, 10, 250, "Turtle System 2."),
            ParamSpec("exit_days", "Exit channel (sessions)", 20, 5, 120, "Turtle System 2."),
        ),
        fn=donchian_breakout,
        warmup=lambda p: p["entry_days"],
        min_ready=lambda p, n: 1,
        caveats=(
            "The Turtles traded a basket of futures with volatility-based sizing and pyramiding; "
            "this is the entry and exit rule only.",
            "Most breakouts fail. The rule relies on a few large trends paying for many "
            "small losses.",
        ),
    ),
    LibraryStrategy(
        id="dual_momentum",
        name="Dual momentum",
        family="Momentum rotation",
        cadence="monthly",
        summary=(
            "Each month hold the single strongest risk asset over the past year - unless "
            "even that one is negative, in which case hold the shelter asset."
        ),
        rules=(
            "The LAST symbol in the list is the shelter (bonds); every other symbol competes.",
            "On the last session of each month, rank the competitors by 252-session return.",
            "If the best return is positive, hold that asset 100%. Otherwise hold the "
            "shelter 100%.",
        ),
        evidence=(_ANTONACCI, _JT),
        published=date(2013, 1, 1),
        params=(
            ParamSpec(
                "lookback_days", "Lookback (sessions)", 252, 60, 504, "Antonacci's 12 months."
            ),
        ),
        fn=dual_momentum,
        warmup=lambda p: p["lookback_days"],
        min_ready=lambda p, n: max(1, n - 1),
        min_symbols=2,
        caveats=(
            "Antonacci compares against the T-bill return; this version uses zero as the hurdle "
            "because no T-bill series is loaded.",
            "Always 100% in one asset: concentrated, and late at sharp turns such as 2020.",
        ),
    ),
    LibraryStrategy(
        id="momentum_rotation",
        name="Cross-sectional momentum rotation",
        family="Momentum rotation",
        cadence="monthly",
        summary=(
            "Each month hold the few strongest names in a list, ranked by 12-month return "
            "skipping the most recent month."
        ),
        rules=(
            "On the last session of each month, rank every asset by its return from 252 to 21 "
            "sessions ago (the latest month is skipped: it tends to reverse).",
            "Hold the top N equally. With the positive-only switch on, a top-N name with "
            "negative momentum is left as cash.",
        ),
        evidence=(_JT, _CTW),
        published=date(1993, 3, 1),
        params=(
            ParamSpec("top_n", "Names held", 3, 1, 20, "How many of the strongest to hold."),
            ParamSpec("lookback_days", "Lookback (sessions)", 252, 60, 504, "12 months."),
            ParamSpec("skip_days", "Skip latest (sessions)", 21, 0, 63, "1 month."),
            ParamSpec(
                "require_positive",
                "Positive momentum only (1 = yes)",
                1,
                0,
                1,
                "Leaves a slot in cash rather than buying the least-bad loser.",
            ),
        ),
        fn=momentum_rotation,
        warmup=lambda p: p["lookback_days"],
        min_ready=lambda p, n: min(n, p["top_n"] + 1),
        min_symbols=2,
        caveats=(
            "The paper is long winners AND short losers across thousands of stocks. Long-only "
            "on a short list keeps most of the market risk.",
            "Momentum has been markedly weaker in Asian markets than in the US and Europe "
            "(Chui, Titman, Wei 2010).",
            "Momentum crashes: it loses badly in sharp rebounds, as in 2009.",
        ),
    ),
    LibraryStrategy(
        id="rsi2_reversion",
        name="RSI(2) pullback in an uptrend",
        family="Short-term reversal",
        cadence="daily",
        summary=("In an uptrend, buy a sharp two-day dip and sell the first bounce."),
        rules=(
            "Enter when the close is above the 200-day average AND the 2-day RSI is under 10.",
            "Exit when the close is back above the 5-day average.",
            "Each asset has an equal fixed share; trade on the next session's close.",
        ),
        evidence=(_CONNORS, _LEHMANN),
        published=date(2008, 11, 1),
        params=(
            ParamSpec("entry_rsi", "Enter below RSI(2)", 10, 2, 40, "Connors used 5 to 10."),
            ParamSpec("trend_days", "Trend average (sessions)", 200, 50, 400, "Uptrend filter."),
            ParamSpec("exit_sma_days", "Exit average (sessions)", 5, 2, 30, "Sell the bounce."),
        ),
        fn=rsi2_reversion,
        warmup=lambda p: p["trend_days"],
        min_ready=lambda p, n: 1,
        caveats=(
            "The source is a practitioner book, not peer-reviewed. The academic short-term "
            "reversal effect is real but is mostly consumed by trading costs.",
            "The book buys ON the signal close. This backtest buys one session later, which "
            "is what an end-of-day process can actually do, and it costs much of the edge.",
            "No stop: the rule holds through a continuing fall until the bounce comes.",
        ),
    ),
)

BASELINE = LibraryStrategy(
    id="buy_and_hold",
    name="Buy and hold (equal weight)",
    family="Baseline",
    cadence="monthly",
    summary="Hold every asset equally, rebalanced monthly. What a strategy must beat.",
    rules=(
        "Hold each listed asset at an equal weight; rebalance on the last session of each month.",
    ),
    evidence=(),
    published=date(1900, 1, 1),
    params=(),
    fn=equal_weight,
    warmup=lambda p: 0,
    min_ready=lambda p, n: 1,
)

_BY_ID = {s.id: s for s in (*STRATEGIES, BASELINE)}


def get_strategy(strategy_id: str) -> LibraryStrategy:
    try:
        return _BY_ID[strategy_id]
    except KeyError:
        raise KeyError(f"Unknown library strategy {strategy_id!r}.") from None


# --------------------------------------------------------------------------
# Universes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    market: str
    symbols: tuple[str, ...]
    benchmark: tuple[str, ...]
    strategies: tuple[str, ...]
    note: str = ""
    survivorship: bool = False
    """True when the list is TODAY's index members. Names that fell out of
    the index or were delisted are missing, which flatters any backtest."""


_SINGLE = ("trend_200d", "tsmom_12m", "donchian_breakout", "rsi2_reversion")

PRESETS: tuple[Preset, ...] = (
    Preset("us_spy", "US large caps - SPY", "US", ("SPY.US",), ("SPY.US",), _SINGLE),
    Preset("us_qqq", "US Nasdaq 100 - QQQ", "US", ("QQQ.US",), ("QQQ.US",), _SINGLE),
    Preset(
        "us_multi_asset",
        "US-listed asset classes",
        "US",
        ("SPY.US", "EFA.US", "IEF.US", "VNQ.US", "GLD.US"),
        ("SPY.US", "EFA.US", "IEF.US", "VNQ.US", "GLD.US"),
        ("trend_200d", "tsmom_12m", "momentum_rotation"),
        note="US stocks, developed ex-US stocks, 7-10y Treasuries, real estate, gold.",
    ),
    Preset(
        "us_dual",
        "US / international / bonds",
        "US",
        ("SPY.US", "EFA.US", "AGG.US"),
        ("SPY.US",),
        ("dual_momentum",),
        note="AGG (US aggregate bonds) is the shelter.",
    ),
    Preset(
        "us_sectors",
        "US sector ETFs",
        "US",
        ("XLB.US", "XLE.US", "XLF.US", "XLI.US", "XLK.US", "XLP.US", "XLU.US", "XLV.US", "XLY.US"),
        ("SPY.US",),
        ("momentum_rotation", "rsi2_reversion"),
    ),
    Preset("sg_sti", "Singapore - STI ETF", "SG", ("ES3.SG",), ("ES3.SG",), _SINGLE),
    Preset(
        "sg_bluechips",
        "Singapore blue chips",
        "SG",
        (
            "D05.SG",
            "O39.SG",
            "U11.SG",
            "Z74.SG",
            "C6L.SG",
            "BN4.SG",
            "C38U.SG",
            "A17U.SG",
            "S68.SG",
            "F34.SG",
            "G13.SG",
            "S63.SG",
            "C09.SG",
            "U96.SG",
        ),
        ("ES3.SG",),
        ("momentum_rotation", "rsi2_reversion"),
        survivorship=True,
    ),
    Preset("in_nifty", "India - Nifty 50 ETF", "IN", ("NIFTYBEES.IN",), ("NIFTYBEES.IN",), _SINGLE),
    Preset(
        "in_largecaps",
        "India large caps",
        "IN",
        (
            "RELIANCE.IN",
            "TCS.IN",
            "HDFCBANK.IN",
            "INFY.IN",
            "ICICIBANK.IN",
            "HINDUNILVR.IN",
            "ITC.IN",
            "SBIN.IN",
            "BHARTIARTL.IN",
            "KOTAKBANK.IN",
            "AXISBANK.IN",
            "BAJFINANCE.IN",
            "ASIANPAINT.IN",
            "MARUTI.IN",
            "SUNPHARMA.IN",
            "TITAN.IN",
        ),
        ("NIFTYBEES.IN",),
        ("momentum_rotation", "rsi2_reversion"),
        survivorship=True,
    ),
    Preset(
        "crypto_btc",
        "Bitcoin",
        "Crypto",
        ("BTC-USD",),
        ("BTC-USD",),
        ("trend_200d", "tsmom_12m", "donchian_breakout"),
    ),
    Preset(
        "crypto_majors",
        "Bitcoin, Ether, Solana",
        "Crypto",
        ("BTC-USD", "ETH-USD", "SOL-USD"),
        ("BTC-USD",),
        ("trend_200d", "tsmom_12m", "donchian_breakout"),
        survivorship=True,
        note="Three coins that survived. Most coins from any earlier year did not.",
    ),
    Preset(
        "fx_majors",
        "FX majors",
        "FX",
        ("EURUSD.FX", "GBPUSD.FX", "AUDUSD.FX", "USDJPY.FX", "USDCAD.FX", "USDCHF.FX"),
        (),
        ("tsmom_12m", "donchian_breakout"),
        note=(
            "Spot rates only: overnight financing (the carry) is not in the data, and it is "
            "a large part of any FX position's real return. There is no buy-and-hold benchmark."
        ),
    ),
    Preset(
        "us_gold",
        "Gold - GLD",
        "US",
        ("GLD.US",),
        ("GLD.US",),
        ("trend_200d", "tsmom_12m", "donchian_breakout"),
    ),
)

_PRESET_BY_ID = {p.id: p for p in PRESETS}


def get_preset(preset_id: str) -> Preset:
    try:
        return _PRESET_BY_ID[preset_id]
    except KeyError:
        raise KeyError(f"Unknown universe {preset_id!r}.") from None


# --------------------------------------------------------------------------
# Costs
# --------------------------------------------------------------------------

COST_BPS: dict[str, float] = {
    "US": 3.0,
    "SG": 15.0,
    "IN": 15.0,
    "HK": 15.0,
    "CRYPTO": 40.0,
    "FX": 2.0,
}
"""One-way cost per trade, in basis points of the amount traded.

US 3: zero commission, about half the spread on a liquid ETF plus fees.
SG 15: roughly 0.08% commission plus clearing and trading fees.
IN 15: delivery trades pay 0.1% securities transaction tax each way, plus
stamp duty and exchange charges - brokerage is the small part.
Crypto 40: a taker fee at a retail tier on Coinbase or Kraken.
FX 2: about one pip on a major. Financing is NOT included.
"""
OTHER_COST_BPS = 15.0


def market_of(symbol: str) -> str:
    raw = symbol.upper()
    if is_fx_symbol(raw):
        return "FX"
    if "." not in raw:
        return "CRYPTO" if ("-" in raw or "/" in raw) else "OTHER"
    return raw.rpartition(".")[2]


def default_cost_bps(symbol: str) -> float:
    return COST_BPS.get(market_of(symbol), OTHER_COST_BPS)
