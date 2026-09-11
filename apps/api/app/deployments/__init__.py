"""Phase 63: putting a validated StrategyVersion on a scheduled paper-trading
runner, behind a mandatory human-approval gate.

`service.py` owns the deployment lifecycle state machine (create -> approve
-> active <-> paused -> stopped); `runner.py` is the in-process periodic
task that re-evaluates every ACTIVE deployment and submits paper trades
through the one sanctioned `oms.service.submit_trade()` path. Nothing here
touches live money - `mode` is `paper` for every Phase-63 row and the API
rejects anything else until Phase 64.
"""
