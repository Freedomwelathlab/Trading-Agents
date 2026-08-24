# Model Routing

One LLM provider path is wired (`AnthropicCompatibleProvider`, D018 —
provider-name-agnostic, points at OmniRoute or any compatible endpoint;
`packages/llm_providers/` is still an empty placeholder, unused so far).
This defines the tiering policy every agent routes through.

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
  `TraderAgent` follows this: `LLM_PROVIDER_MODEL` is settings-driven
  (D018), never referenced by name in `apps/api/app/agents/trader.py`.
  `TraderAgent` itself is STANDARD tier (trade-idea drafting) — the
  actual model that tier maps to is whatever `LLM_PROVIDER_MODEL` names.
- The Risk Engine and any deterministic financial calculation never routes
  through this system at all — it isn't an LLM call in the first place (see
  [TOKEN_POLICY.md](TOKEN_POLICY.md)).
