"""Strategy definitions (Phase 54): the closed vocabulary a strategy's
rules may be written in (`models.py`), the purely structural validator that
checks one against it (`validation.py`), and the deterministic helpers the
routes lean on (`service.py`).

Nothing in this package executes a strategy, reads a price, or touches the
bar store - Phase 55's engine does that. This package answers exactly one
question: "is this JSON a well-formed strategy definition, and if not,
precisely what is wrong with it?"
"""
