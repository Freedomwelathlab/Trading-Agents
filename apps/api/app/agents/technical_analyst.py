"""The parallel analyst layer's first (and, this phase, only) analyst
(docs/DECISIONS.md D019). Single responsibility per docs/AGENT_POLICY.md:
this agent reads the current live quote for a symbol and writes a short,
qualitative technical read - it never proposes a trade, never sizes a
position, and never computes an indicator (RSI/MACD/moving averages/etc)
itself. Any real indicator must be computed deterministically in code from
real historical data per docs/TOKEN_POLICY.md's mandatory-deterministic
list; this codebase has no historical price series wired yet
(packages/data_providers/ is an empty placeholder), so this agent's LLM
call is honestly framed as qualitative commentary on a single live quote,
not technical-indicator analysis.

Never authoritative (docs/TRADING_SAFETY.md spec Sec62, docs/AGENT_POLICY.md):
this agent has no order-submission path at all - TechnicalRead has no
side/quantity/price field, so there is nothing here for a caller to
mistake for a trade proposal. It is optional additional context a caller
may hand to TraderAgent.propose(); TraderAgent remains the only agent that
proposes side/quantity/stop distance, and the Risk Engine still validates
everything downstream, unchanged.
"""

import json
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, ValidationError

from apps.api.app.agents.parsing import extract_json_object
from apps.api.app.agents.provider import LLMProvider, LLMProviderError

_SYSTEM_PROMPT = """You are a read-only market-commentary assistant for a \
paper trading system. You are given a single live price quote for a \
symbol - not a price history, not a chart, not any computed indicator. \
You never see account balances, positions, or risk limits, and you never \
propose or size a trade; a separate agent and a separate deterministic \
Risk Engine handle that. Do not claim to calculate RSI, MACD, moving \
averages, or any other technical indicator - you were given one price \
point, not a series, so there is nothing to compute. Offer only brief, \
honestly-qualitative commentary on the single quote you were given (e.g. \
round-number proximity, the general question of whether the level looks \
stretched) - never invent a trend, a support/resistance level, or any \
other data you were not given.

Respond with ONLY a single JSON object, no other text, matching exactly:
{"stance": "bullish" or "bearish" or "neutral", "summary": "<1-3 \
sentences of qualitative commentary on this one quote>", "confidence": \
"<decimal string between 0 and 1>"}
"""


class AnalystOutputError(Exception):
    """The provider responded, but not with a parseable/valid technical
    read. Never a reason to invent one - the caller must treat this
    analyst's context as unavailable, exactly like NOT_CONFIGURED."""


class Stance(str, Enum):  # noqa: UP042 (str mixin kept for pydantic/env-var interop)
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class TechnicalRead(BaseModel):
    """Structured, validated analyst output (docs/AGENT_POLICY.md: output
    schema, not free text a downstream consumer parses ad hoc). Read-only
    by construction - no side, quantity, price, or stop field exists, so
    there is no field a caller could mistake for a trade proposal."""

    stance: Stance
    summary: str = Field(min_length=1, max_length=500)
    confidence: Decimal = Field(ge=0, le=1)


class TechnicalAnalyst:
    """Model tier: STANDARD (docs/MODEL_ROUTING.md) - short qualitative
    commentary on one quote, not complex multi-source research synthesis.

    Deliberately read-only: analyze() takes the symbol and the price/as-of
    already fetched via the deterministic live-quote path (D017) - this
    class never fetches or chooses a price itself, and never sees more
    than the one quote it was handed.
    """

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def analyze(self, *, symbol: str, price: Decimal, as_of: str) -> TechnicalRead:
        user_prompt = f"Symbol: {symbol}\nCurrent price: {price}\nQuote as of: {as_of}"
        try:
            raw = await self._provider.complete(
                system=_SYSTEM_PROMPT, user=user_prompt, max_tokens=300
            )
        except LLMProviderError as exc:
            raise AnalystOutputError(f"LLM provider call failed: {exc}") from exc

        try:
            parsed = json.loads(extract_json_object(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            raise AnalystOutputError(f"LLM response was not valid JSON: {raw!r}") from exc

        try:
            return TechnicalRead.model_validate(parsed)
        except ValidationError as exc:
            raise AnalystOutputError(f"LLM response failed schema validation: {exc}") from None


def build_technical_analyst(provider: LLMProvider | None) -> "TechnicalAnalyst | None":
    """None means no LLM provider is configured (D018/D019) - callers must
    treat that exactly like the trader agent's own NOT_CONFIGURED
    convention: optional context that's simply unavailable, never a reason
    to block or fabricate anything."""
    if provider is None:
        return None
    return TechnicalAnalyst(provider)
