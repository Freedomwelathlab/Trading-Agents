from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class QuoteResponse(BaseModel):
    symbol: str
    price: Decimal
    as_of: datetime
    source: str
