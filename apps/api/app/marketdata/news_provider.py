"""Recent-news port (D059) - a fourth market-data capability, separate
from quotes/history/fundamentals for the same reason those are separate
from each other. Same no-fabrication posture and the same reused
DataUnavailableError/VendorError types as marketdata/provider.py.

A NewsHeadline is deliberately thin: a title, a real publication
timestamp, and (when the vendor gave one) a URL. No sentiment score, no
"impact" rating, no category - none of those exist in the vendor
response, and deriving one would mean inventing it. See D059's scope cut
on SentimentAnalyst for the same reasoning applied one level up.
"""

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, Field, field_validator


class NewsHeadline(BaseModel):
    title: str = Field(min_length=1)
    published_at: datetime
    url: str | None = None

    @field_validator("published_at")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        return v


class NewsProvider(Protocol):
    name: str

    async def get_recent_headlines(self, symbol: str, *, limit: int) -> list[NewsHeadline]:
        """Returns up to `limit` headlines, NEWEST FIRST. May return fewer
        than `limit` - callers must report the real count they were given,
        never pad the list or imply more coverage than the vendor had.

        Raises DataUnavailableError when the vendor has no news at all for
        the symbol, VendorError when the vendor itself failed.
        """
        ...
