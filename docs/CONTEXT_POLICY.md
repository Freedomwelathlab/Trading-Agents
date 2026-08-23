# Context Policy

Applies to (a) agent-to-agent context once the agent layer exists, and
(b) Claude Code's own exploration of this repo today.

## Context hierarchy

STATIC (this doc set) → DYNAMIC (current task's diffs/state) → RETRIEVED
(specific files fetched on demand) → SESSION (this conversation) →
LONG-TERM MEMORY (Claude's cross-session memory, decisions log).

## Rules for Claude Code working in this repo

- Read `CLAUDE.md` → `docs/PROJECT_CONTEXT.md` → `docs/IMPLEMENTATION_STATUS.md`
  → `docs/CODE_MAP.md` → only the source files the task touches. Don't
  re-scan the whole tree per task.
- The conversation is not the permanent record — this doc set is. Don't
  reconstruct project history from chat scrollback when it's already written
  down here.

## Rules for future agent-to-agent context (not yet built)

- Never send an analyst agent the full portfolio state if it only needs the
  current symbol's data.
- Never send duplicate market data to multiple agents — fetch once through
  the market-data service, pass a reference/snapshot ID.
- Never send one agent's full raw output to another when a summary suffices;
  use structured fields the consumer actually reads.
