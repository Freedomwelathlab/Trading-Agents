"""The first (and, this phase, only) agent: proposes a trade's side,
quantity, and stop distance from a symbol + free-text directive. Single
responsibility per docs/AGENT_POLICY.md - this agent does not analyze,
research, or size against portfolio risk; it only drafts a proposal.

Never authoritative (docs/TRADING_SAFETY.md spec Sec62, docs/AGENT_POLICY.md):
price always comes from the deterministic market-data path (D017), and
every field this agent proposes is re-validated by the same Risk Engine
a human-submitted trade goes through - nothing here can size, price, or
approve a trade on its own.
"""

import json
from decimal import Decimal

from pydantic import BaseModel, Field, ValidationError

from apps.api.app.agents.provider import LLMProvider, LLMProviderError
from apps.api.app.risk.models import Side

_SYSTEM_PROMPT = """You are a trade-proposal drafting assistant for a paper \
trading system. You never see account balances, positions, or risk limits - \
a separate deterministic Risk Engine validates and can reject anything you \
propose. Your only job is to translate a symbol and a free-text directive \
into a structured trade idea.

Respond with ONLY a single JSON object, no other text, matching exactly:
{"side": "buy" or "sell", "quantity": "<positive decimal string>", \
"stop_distance_pct": "<positive decimal string, percent of price, e.g. \
\\"2.5\\" for a 2.5% stop>", "rationale": "<one sentence>"}
"""


class AgentOutputError(Exception):
    """The provider responded, but not with a parseable/valid trade idea.
    Never a reason to invent one - the caller must fail the request."""


class TradeIdea(BaseModel):
    """Structured, validated agent output (docs/AGENT_POLICY.md: output
    schema, not free text a downstream consumer parses ad hoc). Still not
    a TradeProposal - has no price, no market_data_as_of; the caller must
    combine this with a deterministic quote before it means anything."""

    side: Side
    quantity: Decimal = Field(gt=0)
    stop_distance_pct: Decimal = Field(gt=0, le=50)
    rationale: str = Field(min_length=1, max_length=500)


class TraderAgent:
    """Model tier: STANDARD (docs/MODEL_ROUTING.md) - routine trade-idea
    drafting, not complex multi-source research synthesis. The tier is a
    property of which model the configured provider points at
    (LLM_PROVIDER_MODEL), not something this class chooses."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def propose(self, *, symbol: str, directive: str) -> TradeIdea:
        user_prompt = f"Symbol: {symbol}\nDirective: {directive}"
        try:
            raw = await self._provider.complete(
                system=_SYSTEM_PROMPT, user=user_prompt, max_tokens=300
            )
        except LLMProviderError as exc:
            raise AgentOutputError(f"LLM provider call failed: {exc}") from exc

        try:
            parsed = json.loads(_extract_json_object(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            raise AgentOutputError(f"LLM response was not valid JSON: {raw!r}") from exc

        try:
            return TradeIdea.model_validate(parsed)
        except ValidationError as exc:
            raise AgentOutputError(f"LLM response failed schema validation: {exc}") from None


def _extract_json_object(text: str) -> str:
    """Models frequently wrap JSON in prose or code fences despite
    instructions. Take the first {...} span rather than trusting the
    whole response is bare JSON - still fails loudly (AgentOutputError)
    if no valid object is found, never silently guesses a default."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in LLM response")
    return text[start : end + 1]


def build_trader_agent(provider: LLMProvider | None) -> "TraderAgent | None":
    """None means no LLM provider is configured (D018) - callers must
    render that as NOT_CONFIGURED, matching the market-data (D008/D015)
    and Longbridge (D015) conventions exactly."""
    if provider is None:
        return None
    return TraderAgent(provider)


def stop_price_from_distance(*, price: Decimal, side: Side, stop_distance_pct: Decimal) -> Decimal:
    """Deterministic, not agent output - the agent proposes a distance
    (a risk-shape decision), this converts it against the real live price
    (docs/AGENT_POLICY.md: no agent output is authoritative for price)."""
    offset = price * (stop_distance_pct / Decimal(100))
    if side is Side.BUY:
        return price - offset
    return price + offset
