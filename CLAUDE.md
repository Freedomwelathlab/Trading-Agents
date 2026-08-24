PROJECT:
AI-powered multi-asset trading platform ("Trading OS"). Phases 1-15 done
(risk engine, order path, auth, admin API, market data, first agent) — see
docs/IMPLEMENTATION_STATUS.md for exact status, don't assume from this file.

PRIMARY OBJECTIVE:
Build a production-grade AI trading operating system with strict
deterministic risk controls. See ../ARCHITECTURE-DISCOVERY-REPORT.md and the
governing spec (../MASTER CLAUDE CODE PROMPT — WALL STREET AI TRADING
OPERATING SYSTEM.md) for full context.

DOCUMENTATION (read only what the task needs):
docs/PROJECT_CONTEXT.md      — canonical compressed project history
docs/ARCHITECTURE.md         — current + target system architecture
docs/CODE_MAP.md             — what exists, file by file
docs/MODULE_MAP.md           — system-wide module status
docs/AI_OPTIMIZATION.md      — LLM efficiency policy (forward-looking, no agents yet)
docs/TOKEN_POLICY.md         — token budget policy (forward-looking)
docs/MODEL_ROUTING.md        — model tier policy (forward-looking)
docs/CONTEXT_POLICY.md       — what context to send where
docs/AGENT_POLICY.md         — agent design rules (forward-looking)
docs/TRADING_SAFETY.md       — NON-NEGOTIABLE safety rules, read this one fully
docs/IMPLEMENTATION_STATUS.md — what's done, in progress, blocked, planned
docs/DECISIONS.md            — why past decisions were made; don't re-litigate
docs/API.md                  — actual implemented endpoints only
docs/DEVELOPMENT_WORKFLOW.md — the loop to follow for every task

WORKING RULE:
Read only the documentation and source files relevant to the current task.
Do not repeatedly scan the entire repository. Reuse existing code before
writing new code. Prefer deterministic code over LLM calls for any
numerical/financial calculation. Use structured outputs for agent
communication once agents exist. Parallelize independent work. Run targeted
tests first, full suite (pytest + ruff + mypy) before calling a change done.
Never claim functionality exists unless it's implemented and verified —
this applies to phase reports too.

TRADING SAFETY (full detail in docs/TRADING_SAFETY.md):
LLMs must never bypass the deterministic Risk Engine. LLMs must never
directly execute unrestricted live trades. Never enable live trading without
explicit user approval given in that moment. Never expose secrets — check
docs/TRADING_SAFETY.md's redaction note before logging anything
credential-adjacent. Never fabricate market data, orders, fills, portfolio
values, or broker responses — use NOT_CONFIGURED / DATA_UNAVAILABLE
sentinels. Fail closed whenever trading safety cannot be verified.

CONTEXT:
Maintain compact project context in docs/PROJECT_CONTEXT.md. Update project
documentation when architecture or major decisions change — not for every
commit. The conversation is temporary; this doc set is the persistent
memory of the project.
