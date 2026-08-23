# AI Optimization Policy

Applies once the agent/LLM layer exists (not yet built — no LLM call exists
in this codebase today). Written now so Phase 2+ agent code is built against
a decided policy instead of retrofitting one later.

## Objectives

Reduce token usage, latency, and cost without weakening correctness, safety,
or auditability. Token savings never outrank capital safety —
see [TRADING_SAFETY.md](TRADING_SAFETY.md).

## Core rules (apply when writing agent code)

1. Before adding an LLM call, ask: can deterministic code do this? If yes,
   write deterministic code — see the mandatory list in
   [TOKEN_POLICY.md](TOKEN_POLICY.md).
2. Route by task difficulty, not by default-to-strongest — see
   [MODEL_ROUTING.md](MODEL_ROUTING.md).
3. Send only the context an agent needs — see [CONTEXT_POLICY.md](CONTEXT_POLICY.md).
4. Cache safe, repeatable results with source/timestamp/TTL/version. Never
   cache anything used for a live execution decision without a freshness
   check.
5. Batch tool calls (`get_prices([...])`, not N calls to `get_price(...)`).
6. Parallelize independent agents (e.g. technical/fundamental/news/sentiment
   analysts have no cross-dependency — run them concurrently).
7. Use adaptive research depth: early-exit when confidence is high and
   analyst disagreement is low; add rounds when disagreement is high, up to
   a configured max.
8. Use structured outputs (typed schemas) for inter-agent payloads where the
   downstream consumer needs to parse the result programmatically.

## Measurement

No LLM calls exist yet, so there is no baseline to benchmark
(`docs/OPTIMIZATION_BENCHMARK.md` intentionally not created — a before/after
table with no "before" would be fabricated data, which spec §57 forbids in
spirit even for internal docs). Create it once the first agent ships, with
real measured numbers.
