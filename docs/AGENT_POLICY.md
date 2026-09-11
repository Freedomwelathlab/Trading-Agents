# Agent Policy

Agents in this codebase: `TraderAgent` (D018), a single-responsibility
trade-idea drafter; `TechnicalAnalyst` (D019), `FundamentalAnalyst` (D059),
and `NewsAnalyst` (D059), single-responsibility read-only analysts; and
`StrategyResearchAssistant` (D085), a single-responsibility strategy-draft
proposer — none of these is the full parallel-analyst/research-debate stack
below. Like every analyst, `StrategyResearchAssistant` has no order or
trade-submission path at all: its entire output is a proposed
`StrategyDefinition` draft plus a rationale, both structurally validated
against the same closed vocabulary a human-authored strategy must pass
(`apps/api/app/strategies/validation.py::validate_definition`) before being
handed back — never persisted, never a claim of backtested merit. This
remains the policy every future agent is designed against.

## Rules

- Each agent is a specialist with one responsibility (e.g. "technical
  analyst," not "analyst that also does risk"). Don't build a do-everything
  agent.
- Every agent defines: input requirements, output schema (structured, not
  free text where a downstream consumer parses it), model tier (see
  [MODEL_ROUTING.md](MODEL_ROUTING.md)), and failure handling.
- Agent selection is task-dependent — e.g. an intraday-equity workflow
  doesn't need the same analyst set as a long-horizon one. Don't run
  agents whose output the current task can't use.
- Parallelize agents with no cross-dependency (see [AI_OPTIMIZATION.md](AI_OPTIMIZATION.md)).
- No agent output is ever authoritative for price, balance, position,
  P&L, or risk — see [TRADING_SAFETY.md](TRADING_SAFETY.md) §62.
- On failure: determine if transient, retry only if justified, prefer a
  deterministic fallback over a second LLM call, never an uncontrolled
  retry loop.
