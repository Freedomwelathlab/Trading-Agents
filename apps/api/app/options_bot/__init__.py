"""The options paper bot (Phase 102, D122).

A sibling of `apps/api/app/autotrade/` with the same safety model: created
PENDING_APPROVAL, ACTIVE only by an explicit separately-permissioned
approval, paper brokers only, every cycle a terminal run row, and every
order through the one option order path (`options/paper_book.py`), which
puts every opening order through the deterministic Risk Engine.
"""
