# Model Routing

No LLM provider is wired into the codebase yet
(`packages/llm_providers/` is an empty placeholder). This defines the tiering
policy to implement when it is.

## Tiers

**FAST** — classification, extraction, simple summaries, formatting.
**STANDARD** — routine analysts, news synthesis, trade-idea drafting.
**HIGH REASONING** — complex/conflicting research synthesis, portfolio-level
decisions.
**CRITICAL** — reserved for cases that clearly justify the top-tier model;
never the default.

## Rules

- Never default to the most expensive model.
- Model names must be configurable (env/config), not hard-coded through
  business logic — TradingAgents' `llm_clients/factory.py` pattern is worth
  reusing here (see `ARCHITECTURE-DISCOVERY-REPORT.md` Part II).
- The Risk Engine and any deterministic financial calculation never routes
  through this system at all — it isn't an LLM call in the first place (see
  [TOKEN_POLICY.md](TOKEN_POLICY.md)).
