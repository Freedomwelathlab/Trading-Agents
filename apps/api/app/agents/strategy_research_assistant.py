"""The Strategy Lab's advisory research agent (Phase 67, D085): given a
short natural-language research brief (e.g. "a mean-reversion idea using
RSI"), proposes a candidate `StrategyDefinition` draft plus a plain-English
rationale for a human to review.

Never authoritative, exactly like every other analyst (docs/TRADING_SAFETY.md
spec Sec62, docs/AGENT_POLICY.md): this agent has no trade-submission path at
all. `StrategyProposal` has no side/quantity/price/stop field, and `propose()`
never calls a broker, the Risk Engine, or anything downstream of it - there is
nothing here for a caller to mistake for a trade, and nothing to bypass. It is
one step further removed from execution than `TraderAgent` even: TraderAgent's
output can become a real order once the Risk Engine approves it; this agent's
output can only ever become a `StrategyVersion` DRAFT row, which itself does
nothing until a human validates it, deploys it, and the existing paper-trading
runner (Phase 63, D081) executes it behind its own mandatory approval gate.
Three human decisions and two deterministic gates sit between this agent's
output and a single order.

**`validate_definition` is reused VERBATIM, never re-specified or loosened.**
The closed vocabulary in `apps/api/app/strategies/models.py` is the actual
safety boundary on this platform (see that module's and
`apps/api/app/strategies/validation.py`'s own docstrings: no strategy
definition may ever be a vector for code execution). The system prompt below
states that vocabulary explicitly so the LLM has the best chance of proposing
something that validates on the first try, but the prompt is advisory to the
model, not a contract this code trusts. Every proposed definition is run
through the same structural validator every manually-authored strategy must
pass before this agent hands it back - this agent must be structurally unable
to return something that looks like a valid `StrategyDefinition` but isn't.

**An invalid draft is a normal, expected result - never an exception.**
`validate_definition`'s own docstring says an empty `{}` is a legal draft to
store but not a valid definition; the same posture applies here. If the LLM's
draft fails structural validation, `StrategyProposal.validation_errors` carries
the real, itemized errors and `is_valid` is `False` - this is exactly as
informative to the caller as any other rejected draft, and raising here would
turn an ordinary "the model proposed something malformed, here is why" into
an outage. The one thing this agent DOES raise `AnalystOutputError` for is a
response that cannot even be parsed as the expected JSON shape (unparseable
JSON, or a response missing `name`/`definition`/`rationale` entirely) -
that is a provider/format failure, not a structural verdict on a strategy,
and matches `TechnicalAnalyst`'s and every other analyst's identical
try/except shape for that class of failure.

**This agent never creates a `Strategy` or `StrategyVersion` row - zero
persistence side effects.** `propose()` does not open a database session and
`POST /strategies/research/propose` (apps/api/app/api/routes/strategies.py)
performs no database write of any kind. A human reviews the name, definition,
and rationale this agent returns and, if they choose to keep it, creates the
strategy for real through the existing `POST /strategies` +
`POST /strategies/{id}/versions/{id}/validate` path - at which point
`validate_definition` runs again, for real, against the row that will
actually be used. Showing a draft and saving one are deliberately two
different, human-gated acts (matching the "no code path skips a deliberate
human action" thread running through D071/D081): nothing about a
plausible-sounding idea should be able to become a persisted, runnable
strategy without a person choosing to put it there.

**Never a claim of merit.** The prompt explicitly forbids stating or implying
a backtest result, a profitability claim, or any performance number - this
agent has run no backtest and computed no indicator value itself, exactly
like every other analyst in this codebase (D019, D059). The rationale may
only describe the mechanical idea: what triggers entry/exit and why that is a
coherent rule to write down, never whether it would have worked.
"""

import enum
import json

from pydantic import BaseModel, Field, ValidationError

from apps.api.app.agents.parsing import extract_json_object
from apps.api.app.agents.provider import LLMProvider, LLMProviderError
from apps.api.app.agents.technical_analyst import AnalystOutputError
from apps.api.app.strategies.models import (
    DEFINITION_KEYS,
    INDICATOR_KEYS,
    PRICE_OPERAND_CLOSE,
    REQUIRED_SIZING_KEYS,
    RULE_KEYS,
    IndicatorType,
    PositionSizingType,
    RuleOperator,
)
from apps.api.app.strategies.validation import validate_definition

__all__ = [
    "StrategyProposal",
    "StrategyResearchAssistant",
    "build_strategy_research_assistant",
]


def _vocabulary_line(enum_cls: type[enum.Enum]) -> str:
    return ", ".join(f'"{member.value}"' for member in enum_cls)


def _sizing_line() -> str:
    """Built straight off `REQUIRED_SIZING_KEYS` (apps/api/app/strategies/
    models.py) rather than hand-copied prose, so the prompt cannot drift
    from the closed vocabulary it is describing if that dict ever gains or
    loses a sizing type or a required parameter."""
    parts = []
    for sizing_type, required in REQUIRED_SIZING_KEYS.items():
        if required:
            parts.append(f'"{sizing_type.value}" requires {", ".join(required)!r}')
        else:
            parts.append(f'"{sizing_type.value}" takes no extra parameter')
    return "; ".join(parts)


def _top_level_line() -> str:
    return ", ".join(f'"{key}"' for key in DEFINITION_KEYS)


_SYSTEM_PROMPT = f"""You are a read-only strategy research assistant for a \
paper trading system. Given a short research brief describing a trading \
idea, you propose ONE candidate strategy definition and a plain-English \
rationale for a human to review. You never place, size, or execute a trade; \
a separate agent and a separate deterministic Risk Engine handle that, and \
nothing you produce is ever saved automatically - a human decides whether \
to keep your draft.

A strategy definition uses ONLY this closed vocabulary. Never invent an \
indicator type, operator, or sizing type outside it - an unrecognized value \
is a rejected draft, not a creative addition.

Top-level keys (all four required, no others allowed): {_top_level_line()}.
  "indicators": a list of {{"id": "<unique short id>", "type": <indicator \
type>, "period": <positive integer>}}
  "entry_rule": {{"op": <operator>, "left": <operand>, "right": <operand>}}
  "exit_rule": {{"op": <operator>, "left": <operand>, "right": <operand>}}
  "position_sizing": {{"type": <sizing type>, ...its required parameter}}

Indicator types ("indicators" entries' "type"): \
{_vocabulary_line(IndicatorType)}. Nothing else exists in this system - no \
MACD, no Bollinger Bands, no ATR, no custom formulas.

Operators (a rule's "op"): {_vocabulary_line(RuleOperator)}. The four \
instantaneous comparisons (gt/gte/lt/lte) hold or do not hold on a single \
bar. "crosses_above"/"crosses_below" are NOT the same thing as gt/lt: a \
crossing operator means "did not hold on the previous bar and holds on this \
one" - a signal firing once at the moment of the cross - while gt/lt/gte/lte \
hold continuously for as long as the comparison is true. Choose the one that \
actually matches what you mean the rule to do.

An operand (a rule's "left" or "right") must be EXACTLY one of: the literal \
string "{PRICE_OPERAND_CLOSE}" (the bar's closing price), or the "id" of one \
of your own declared indicators. Nothing else - not a raw number, not an \
indicator type name, not an indicator you didn't declare in "indicators".

Position sizing types ({_vocabulary_line(PositionSizingType)}) and their \
required extra parameter: {_sizing_line()}. "fraction" must be greater than \
0 and at most 1; "amount" must be greater than 0. Include exactly the \
parameter its type requires, nothing extra.

An indicator entry's keys are exactly {INDICATOR_KEYS!r}; a rule's keys are \
exactly {RULE_KEYS!r}. Every indicator "id" must be unique and must be used \
by at least the rule it was declared for.

You have run NO backtest and computed NO real indicator value. Never state \
or imply a backtest result, a win rate, a return, a Sharpe ratio, or any \
other performance number - you have none. Proposing a strategy idea is not a \
claim that it is any good; that is what a real backtest is for, and you are \
not one. Write the rationale as plain English explaining the mechanical \
idea: what condition triggers an entry, what triggers an exit, and why that \
combination is a coherent trading rule to write down - not marketing \
language, not a promise of profit.

Respond with ONLY a single JSON object, no other text, matching exactly:
{{"name": "<short strategy name, 3-8 words>", "definition": {{"indicators": \
[...], "entry_rule": {{...}}, "exit_rule": {{...}}, "position_sizing": \
{{...}}}}, "rationale": "<2-5 sentences, referencing only the vocabulary \
above, no performance claims>"}}
"""


class _RawProposal(BaseModel):
    """The raw shape an LLM response must match before `definition` is even
    extracted for structural validation. This is deliberately NOT a model of
    the strategy rule language itself (see schemas_strategies.py's own
    docstring for why `definition` stays a plain `dict` everywhere in this
    codebase) - it only confirms the three top-level fields this agent's
    contract promises are present and roughly typed. A response missing one
    of these, or with a `definition` that isn't even a JSON object, is a
    provider/format failure (`AnalystOutputError`), not a verdict on the
    strategy - that verdict is `validate_definition`'s job alone, run
    afterward in `StrategyResearchAssistant.propose`."""

    name: str = Field(min_length=1, max_length=200)
    definition: dict
    rationale: str = Field(min_length=1, max_length=1000)


class StrategyProposal(BaseModel):
    """This agent's structured output. `validation_errors` is populated by
    running the SAME `validate_definition` every manually-authored strategy
    must pass (apps/api/app/strategies/validation.py) - never a looser,
    LLM-specific check. An empty list means the proposed `definition` is a
    structurally valid `StrategyDefinition`; a non-empty list is a normal,
    expected result (see this module's docstring), not something `propose()`
    raises over."""

    name: str = Field(min_length=1, max_length=200)
    definition: dict
    rationale: str = Field(min_length=1, max_length=1000)
    validation_errors: list[str] = Field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.validation_errors


class StrategyResearchAssistant:
    """Model tier: STANDARD (docs/MODEL_ROUTING.md) - proposing one strategy
    idea from a short brief, not complex multi-source research synthesis.

    Deliberately read-only and side-effect-free: `propose()` takes only the
    caller's brief, never touches a database session, a broker, or the Risk
    Engine, and returns a `StrategyProposal` the caller may discard, show to
    a human, or (via the existing, separate `POST /strategies` +
    `POST .../versions/{{id}}/validate` path) turn into a real strategy.
    """

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def propose(self, *, brief: str) -> StrategyProposal:
        try:
            raw = await self._provider.complete(
                system=_SYSTEM_PROMPT, user=brief, max_tokens=800
            )
        except LLMProviderError as exc:
            raise AnalystOutputError(f"LLM provider call failed: {exc}") from exc

        try:
            parsed = json.loads(extract_json_object(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            raise AnalystOutputError(f"LLM response was not valid JSON: {raw!r}") from exc

        try:
            candidate = _RawProposal.model_validate(parsed)
        except ValidationError as exc:
            raise AnalystOutputError(f"LLM response failed schema validation: {exc}") from None

        # Beyond this point a malformed draft is a normal result, never an
        # AnalystOutputError - see this module's docstring. validate_definition
        # is called VERBATIM, never reimplemented or loosened; its return
        # value (possibly empty, meaning valid) is copied straight into
        # validation_errors.
        errors = validate_definition(candidate.definition)
        return StrategyProposal(
            name=candidate.name,
            definition=candidate.definition,
            rationale=candidate.rationale,
            validation_errors=errors,
        )


def build_strategy_research_assistant(
    provider: LLMProvider | None,
) -> "StrategyResearchAssistant | None":
    """None means no LLM provider is configured (D018) - matching
    `build_technical_analyst` exactly. Unlike `TechnicalAnalyst`, this agent
    is not optional context for another agent's call; a caller with no
    assistant configured must render that as NOT_CONFIGURED at the route
    layer, the same as `get_trader_agent`'s convention."""
    if provider is None:
        return None
    return StrategyResearchAssistant(provider)
