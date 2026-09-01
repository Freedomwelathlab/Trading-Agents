"""The parallel analyst layer's first (and, this phase, only) analyst
(docs/DECISIONS.md D019, D021). Single responsibility per
docs/AGENT_POLICY.md: this agent reads the current live quote (and,
since D021, real deterministically-computed indicator values when
history is available) for a symbol and writes a short technical read -
it never proposes a trade, never sizes a position, and never computes an
indicator (RSI/MACD/moving averages/etc) itself. Every real indicator
value it narrates was computed by apps/api/app/marketdata/indicators.py
(pure functions, no LLM) per docs/TOKEN_POLICY.md's mandatory-
deterministic list - this agent's job is strictly narration of numbers
it was handed, never calculation. When no history is available (D019's
original, still-supported case), it honestly falls back to qualitative
commentary on the single live quote alone, never inventing an indicator
value it wasn't given.

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
symbol, and sometimes also real, already-computed indicator values \
(e.g. an SMA or RSI) - these were calculated by deterministic code, not \
by you; you never see account balances, positions, or risk limits, and \
you never propose or size a trade; a separate agent and a separate \
deterministic Risk Engine handle that. Never claim to calculate or \
recompute RSI, MACD, moving averages, or any other technical indicator \
yourself, and never state a numeric indicator value that was not given \
to you in this prompt - you may only narrate/interpret values you were \
actually handed. If no indicator values are given, you were given only \
one price point, not a series - offer brief, honestly-qualitative \
commentary on that quote alone (e.g. round-number proximity) and do not \
invent a trend, support/resistance level, or indicator value you were \
not given.

Respond with ONLY a single JSON object, no other text, matching exactly:
{"stance": "bullish" or "bearish" or "neutral", "summary": "<1-3 \
sentences, referencing only data actually given to you>", "confidence": \
"<decimal string between 0 and 1>"}
"""


class AnalystOutputError(Exception):
    """The provider responded, but not with a parseable/valid analyst
    read. Never a reason to invent one - the caller must treat this
    analyst's context as unavailable, exactly like NOT_CONFIGURED.

    Shared by the whole analyst layer, not just this analyst: D059's
    FundamentalAnalyst and NewsAnalyst raise this same type, because the
    failure semantics and the caller's required response (omit the
    context, never fabricate it) are identical. Defined here rather than
    duplicated per analyst so a caller can catch one type for all of
    them."""


class Stance(str, Enum):  # noqa: UP042 (str mixin kept for pydantic/env-var interop)
    """Shared across every analyst read (D019, D059) - the same three
    values mean the same three things regardless of which analyst
    produced them."""


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

    async def analyze(
        self,
        *,
        symbol: str,
        price: Decimal,
        as_of: str,
        indicator_context: str | None = None,
    ) -> TechnicalRead:
        user_prompt = f"Symbol: {symbol}\nCurrent price: {price}\nQuote as of: {as_of}"
        if indicator_context:
            # D021: real, deterministically-computed indicator values
            # (apps/api/app/marketdata/indicators.py) - narration input
            # only, never something this call is asked to calculate.
            user_prompt += f"\nComputed indicators (real, not estimates): {indicator_context}"
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
