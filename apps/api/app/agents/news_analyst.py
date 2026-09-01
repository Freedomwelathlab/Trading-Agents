"""The parallel analyst layer's third analyst (docs/DECISIONS.md D059),
mirroring TechnicalAnalyst (D019/D021) exactly. Single responsibility per
docs/AGENT_POLICY.md: this agent is handed real, already-fetched recent
headlines for one symbol and writes a short news read - it never proposes
a trade, never sizes a position, and never fetches or searches for news
itself.

The no-fabrication rule has a specific shape here. The failure mode for a
news agent is not a wrong arithmetic result, it is an invented article:
a headline that was never published, a date that was never in the
response, or a count larger than the list it was given. So the prompt
carries an explicit, deterministically-computed headline count and date
range (both produced by format_headlines() below, never by the model),
and the system prompt forbids citing anything outside the numbered list
it can see.

Never authoritative (docs/TRADING_SAFETY.md spec Sec62,
docs/AGENT_POLICY.md): NewsRead has no side/quantity/price field. It is
optional additional context a caller may hand to TraderAgent.propose();
the Risk Engine still validates everything downstream, unchanged.
"""

import json
from decimal import Decimal

from pydantic import BaseModel, Field, ValidationError

from apps.api.app.agents.parsing import extract_json_object
from apps.api.app.agents.provider import LLMProvider, LLMProviderError
from apps.api.app.agents.technical_analyst import AnalystOutputError, Stance
from apps.api.app.marketdata.news_provider import NewsHeadline

_SYSTEM_PROMPT = """You are a read-only news-flow commentary assistant for \
a paper trading system. You are given a numbered list of real, recent \
headlines for one symbol, each with its real publication date, plus a \
count and date range that were computed deterministically from that exact \
list. You never see account balances, positions, or risk limits, and you \
never propose or size a trade; a separate agent and a separate \
deterministic Risk Engine handle that.

Cite only headlines that appear in the list below. Never mention an \
article, event, earnings result, rumour, or date that is not in the list, \
even if you believe you know of one - your knowledge of this company from \
training is not evidence and must not appear in your answer. Never state \
a headline count other than the one given to you, and never claim \
coverage of a period wider than the stated date range. If the headlines \
are few, stale, or not directionally informative, say so plainly and use \
the "neutral" stance.

Respond with ONLY a single JSON object, no other text, matching exactly:
{"stance": "bullish" or "bearish" or "neutral", "summary": "<1-3 \
sentences, referencing only headlines actually listed for you>", \
"confidence": "<decimal string between 0 and 1>"}
"""


class NewsRead(BaseModel):
    """Structured, validated analyst output - the same
    stance/summary/confidence contract as TechnicalRead and
    FundamentalRead, deliberately identical so the trade path treats every
    analyst's read the same way. Read-only by construction."""

    stance: Stance
    summary: str = Field(min_length=1, max_length=500)
    confidence: Decimal = Field(ge=0, le=1)


def format_headlines(symbol: str, headlines: list[NewsHeadline]) -> str:
    """Deterministic rendering of real headlines into the prompt. The
    count and the date range are computed here, in code, from the exact
    list being shown - so the figures the model is told to cite are
    arithmetic facts about its own input rather than something it counted
    (or miscounted) itself.

    `headlines` must be non-empty: an empty list is DATA_UNAVAILABLE at
    the provider layer and never reaches an analyst.
    """
    if not headlines:
        raise ValueError("format_headlines requires at least one real headline")

    published = [headline.published_at for headline in headlines]
    lines = [
        f"Symbol: {symbol}",
        f"Headline count (counted deterministically, this is the exact number "
        f"of items below): {len(headlines)}",
        f"Oldest headline in this set: {min(published).isoformat()}",
        f"Newest headline in this set: {max(published).isoformat()}",
        "",
        "Headlines (newest first):",
    ]
    lines.extend(
        f"{index}. [{headline.published_at.isoformat()}] {headline.title}"
        for index, headline in enumerate(headlines, start=1)
    )
    return "\n".join(lines)


class NewsAnalyst:
    """Model tier: STANDARD (docs/MODEL_ROUTING.md) - short qualitative
    commentary on a short list of headlines, not multi-source research
    synthesis.

    Deliberately read-only: analyze() takes headlines already fetched
    through the deterministic NewsProvider path - this class never
    fetches, searches, or filters news itself.
    """

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def analyze(self, *, symbol: str, headlines: list[NewsHeadline]) -> NewsRead:
        try:
            user_prompt = format_headlines(symbol, headlines)
        except ValueError as exc:
            # No real headlines means no read - never an invented one.
            raise AnalystOutputError(str(exc)) from None

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
            return NewsRead.model_validate(parsed)
        except ValidationError as exc:
            raise AnalystOutputError(f"LLM response failed schema validation: {exc}") from None


def build_news_analyst(provider: LLMProvider | None) -> "NewsAnalyst | None":
    """None means no LLM provider is configured (D018/D019/D059) - callers
    must treat that exactly like the trader agent's own NOT_CONFIGURED
    convention: optional context that's simply unavailable, never a reason
    to block or fabricate anything."""
    if provider is None:
        return None
    return NewsAnalyst(provider)
