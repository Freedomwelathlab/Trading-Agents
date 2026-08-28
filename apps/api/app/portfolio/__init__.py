"""Deterministic portfolio reporting (D022). Reads existing broker_accounts/
broker_positions/orders/fills rows and computes a point-in-time snapshot -
no new mutable state, no LLM, no fabricated prices. See
apps/api/app/portfolio/snapshot.py for the computation and
docs/DECISIONS.md D022 for why."""
