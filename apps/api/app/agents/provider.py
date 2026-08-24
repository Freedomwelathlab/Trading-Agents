"""LLM chat-completion port. No concrete provider is hard-wired into this
codebase - implementing this Protocol is the only sanctioned way to add
one. Mirrors apps/api/app/marketdata/provider.py's shape deliberately:
same optional-vendor, same typed-error, same no-fabrication posture,
just for text completions instead of quotes (docs/DECISIONS.md D018).
"""

from typing import Protocol


class LLMProviderError(Exception):
    """The provider was reachable but failed - HTTP error, auth failure,
    rate limit, malformed response. Never caught and replaced with a
    guessed completion; callers must fail the request, not invent one."""


class LLMProvider(Protocol):
    name: str

    async def complete(self, *, system: str, user: str, max_tokens: int) -> str: ...
