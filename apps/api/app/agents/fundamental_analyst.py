"""The parallel analyst layer's second analyst (docs/DECISIONS.md D059),
mirroring TechnicalAnalyst (D019/D021) exactly. Single responsibility per
docs/AGENT_POLICY.md: this agent is handed real, already-fetched company
fundamentals for one symbol and writes a short fundamental read - it never
proposes a trade, never sizes a position, and never computes a ratio.

Every number it narrates was either returned verbatim by the vendor
(apps/api/app/marketdata/providers/longbridge.py's
LongbridgeFundamentalsProvider) or derived by pure deterministic code
(apps/api/app/marketdata/fundamental_metrics.py), exactly as
indicators.py serves TechnicalAnalyst. The LLM's job here is strictly
narration of numbers it was handed, never calculation and never
estimation - a metric the vendor did not return is rendered to the prompt
as "not reported by the vendor" and must stay that way in the output.

Never authoritative (docs/TRADING_SAFETY.md spec Sec62,
docs/AGENT_POLICY.md): FundamentalRead has no side/quantity/price field,
so there is nothing here a caller could mistake for a trade proposal. It
is optional additional context a caller may hand to TraderAgent.propose();
TraderAgent remains the only agent that proposes side/quantity/stop
distance, and the Risk Engine still validates everything downstream,
unchanged.
"""

import json
from decimal import Decimal

from pydantic import BaseModel, Field, ValidationError

from apps.api.app.agents.parsing import extract_json_object
from apps.api.app.agents.provider import LLMProvider, LLMProviderError
from apps.api.app.agents.technical_analyst import AnalystOutputError, Stance
from apps.api.app.marketdata.fundamental_metrics import earnings_yield_pct
from apps.api.app.marketdata.fundamentals_provider import CompanyFundamentals

_SYSTEM_PROMPT = """You are a read-only company-fundamentals commentary \
assistant for a paper trading system. You are given real, already-fetched \
fundamental figures for one company - they were retrieved from a market \
data vendor and, where derived, calculated by deterministic code, never \
by you. You never see account balances, positions, or risk limits, and \
you never propose or size a trade; a separate agent and a separate \
deterministic Risk Engine handle that.

Never calculate, recalculate, or estimate any ratio, growth rate, margin, \
fair value, or price target yourself, and never state a numeric value \
that was not given to you in this prompt. A field shown as "not reported \
by the vendor" is genuinely unknown - say so if it matters, and never \
substitute an industry norm, a typical value, or a figure you recall for \
this company. If the figures given are too sparse to support a directional \
view, say so and use the "neutral" stance.

Respond with ONLY a single JSON object, no other text, matching exactly:
{"stance": "bullish" or "bearish" or "neutral", "summary": "<1-3 \
sentences, referencing only figures actually given to you>", \
"confidence": "<decimal string between 0 and 1>"}
"""

_NOT_REPORTED = "not reported by the vendor"


def _or_not_reported(value: object) -> str:
    """A real value rendered verbatim, or an explicit absence marker.
    Absence is spelled out rather than left blank so the model cannot
    read a missing line as room to supply the number itself."""
    return _NOT_REPORTED if value is None else str(value)


class FundamentalRead(BaseModel):
    """Structured, validated analyst output - the same contract shape as
    TechnicalRead (stance/summary/confidence), deliberately identical so
    the trade path treats every analyst's read the same way. Read-only by
    construction: no side, quantity, price, or stop field exists."""

    stance: Stance
    summary: str = Field(min_length=1, max_length=500)
    confidence: Decimal = Field(ge=0, le=1)


def format_fundamentals(fundamentals: CompanyFundamentals) -> str:
    """Deterministic rendering of a real fundamentals record into the
    prompt lines the LLM narrates. Pure formatting plus one exact derived
    figure (earnings yield, via fundamental_metrics.earnings_yield_pct) -
    no rounding-to-taste, no defaulting, and every absent metric spelled
    out as absent rather than silently dropped, so the model cannot read
    an omission as an invitation to supply the number itself.
    """
    lines = [
        f"Symbol: {fundamentals.symbol}",
        f"Data source: {fundamentals.source}",
        f"Company name: {_or_not_reported(fundamentals.company_name)}",
        f"Vendor company category: {_or_not_reported(fundamentals.category)}",
        f"P/E: {_or_not_reported(fundamentals.pe)}",
        f"P/B: {_or_not_reported(fundamentals.pb)}",
        f"P/S: {_or_not_reported(fundamentals.ps)}",
        f"Dividend yield: {_or_not_reported(fundamentals.dividend_yield)}",
    ]

    if fundamentals.pe is not None and fundamentals.pe > 0:
        # Deterministic, exact, and computed here rather than asked of
        # the LLM (docs/TOKEN_POLICY.md's mandatory-deterministic rule).
        lines.append(
            f"Earnings yield % (computed deterministically as 100/PE): "
            f"{earnings_yield_pct(fundamentals.pe)}"
        )

    as_of = fundamentals.as_of.isoformat() if fundamentals.as_of else None
    lines.append(f"Valuation figures as of: {_or_not_reported(as_of)}")
    return "\n".join(lines)


class FundamentalAnalyst:
    """Model tier: STANDARD (docs/MODEL_ROUTING.md) - short qualitative
    commentary on a handful of already-fetched figures, not complex
    multi-source research synthesis.

    Deliberately read-only: analyze() takes a CompanyFundamentals record
    that was already fetched through the deterministic
    FundamentalsProvider path - this class never fetches, chooses, or
    filters vendor data itself.
    """

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def analyze(self, *, fundamentals: CompanyFundamentals) -> FundamentalRead:
        try:
            raw = await self._provider.complete(
                system=_SYSTEM_PROMPT, user=format_fundamentals(fundamentals), max_tokens=300
            )
        except LLMProviderError as exc:
            raise AnalystOutputError(f"LLM provider call failed: {exc}") from exc

        try:
            parsed = json.loads(extract_json_object(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            raise AnalystOutputError(f"LLM response was not valid JSON: {raw!r}") from exc

        try:
            return FundamentalRead.model_validate(parsed)
        except ValidationError as exc:
            raise AnalystOutputError(f"LLM response failed schema validation: {exc}") from None


def build_fundamental_analyst(provider: LLMProvider | None) -> "FundamentalAnalyst | None":
    """None means no LLM provider is configured (D018/D019/D059) - callers
    must treat that exactly like the trader agent's own NOT_CONFIGURED
    convention: optional context that's simply unavailable, never a reason
    to block or fabricate anything."""
    if provider is None:
        return None
    return FundamentalAnalyst(provider)
