"""In-process registry of one PaperBrokerAdapter per broker_id.

This is a stopgap, not a design decision to keep: broker state lives only
in this process's memory and is lost on restart, same limitation as
PaperBrokerAdapter itself (docs/DECISIONS.md D005). It exists so the HTTP
layer has something concrete to call without inventing a persisted ledger
this phase didn't scope. Revisit before this app ever runs more than one
worker process - a second worker would get its own, divergent registry.
"""

import uuid
from decimal import Decimal

from apps.api.app.execution.paper_broker import PaperBrokerAdapter


class PaperBrokerRegistry:
    def __init__(self, *, starting_cash: Decimal) -> None:
        self._starting_cash = starting_cash
        self._brokers: dict[uuid.UUID, PaperBrokerAdapter] = {}

    def get_or_create(self, broker_id: uuid.UUID) -> PaperBrokerAdapter:
        if broker_id not in self._brokers:
            self._brokers[broker_id] = PaperBrokerAdapter(starting_cash=self._starting_cash)
        return self._brokers[broker_id]
