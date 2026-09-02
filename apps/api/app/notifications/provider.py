"""Transactional-email port. No concrete vendor is hard-wired into this
codebase - implementing this Protocol is the only sanctioned way to add
one.

Mirrors apps/api/app/marketdata/provider.py and
apps/api/app/agents/provider.py deliberately: same optional-vendor, same
typed-error, same no-fabrication posture (docs/DECISIONS.md D008/D018,
extended to notifications by D062), just for outbound mail instead of
quotes or completions.

The no-fabrication rule matters more here than it looks. Every other
delivery mechanism in this app returns something the caller can inspect; a
sent email does not. So the only two honest outcomes are "the vendor
accepted it" and "it raised", and there is deliberately no third state in
which a caller may assume delivery because nothing complained.
"""

from typing import Protocol


class EmailProviderError(Exception):
    """The provider was reachable but the send failed - HTTP error, auth
    failure, rate limit, rejected recipient, malformed response.

    Never swallowed and reported as a success. A caller either surfaces it
    (the admin endpoint answers 502) or records it in the structured log
    and returns a response that does not claim delivery (the public
    self-service endpoint, which cannot vary its response by outcome
    without leaking whether the address is registered).
    """


class EmailProvider(Protocol):
    name: str

    async def send(self, *, to: str, subject: str, text: str) -> None:
        """Return normally only if the vendor ACCEPTED the message. Raise
        EmailProviderError otherwise. Returning normally is a claim that
        this app makes to its users, so it must not be made on a timeout,
        a 4xx, or an unparseable response."""
        ...
