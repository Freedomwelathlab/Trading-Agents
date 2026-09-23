"""Concrete broker adapters, one module per venue (Phase 92, D111).

Nothing outside `execution/` imports these directly — the registry
(`execution/registry.py`) maps a `brokers.provider` string to a factory,
and every caller depends on the `BrokerAdapter` Protocol. That indirection
is what keeps a paper adapter and a live one structurally interchangeable
and never mixed up by accident.
"""
