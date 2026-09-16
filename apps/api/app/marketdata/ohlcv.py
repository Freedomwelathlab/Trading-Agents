"""The structural read contract for one OHLCV bar (Phase 73, D091).

Session logic, structure detection and the bracket simulator all consume
"a bar" without caring where it came from: a `MarketDataBar` ORM row, a
`Bar` pydantic model from a vendor adapter, or a plain fixture object in
a test. Each of those modules originally declared its own local duck type
for this, which reads fine but means the type checker sees three
unrelated classes and rejects passing a bar from one module to another -
and the obvious way out, casting at every boundary, silences the checker
exactly where a genuine shape mismatch would show up.

One `Protocol` instead. Structural, so nothing has to inherit from it,
and `runtime_checkable` so a caller can assert on it at a boundary where
the input is genuinely unknown.

`open`/`high`/`low` are optional because they genuinely are: a
closes-only vendor series populates `close` alone, and this is the same
reason `indicators.atr` refuses close-only bars rather than estimating a
range from them.
"""

from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable


@runtime_checkable
class OHLCVBar(Protocol):
    ts: datetime
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal
    volume: int | None
