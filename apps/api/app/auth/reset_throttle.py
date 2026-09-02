"""Per-client-IP fixed-window throttle for POST /auth/password-reset/request
(docs/DECISIONS.md D063).

WHAT THIS IS, STATED HONESTLY, BECAUSE IT IS EASY TO OVERSELL:

It is an in-process, per-worker, fixed-window counter. With N uvicorn or
gunicorn workers the real ceiling is N times the configured number, and
every count resets when the process restarts. It imposes a cost on casual
abuse of an unauthenticated endpoint. It is NOT a security boundary and
must not be described as one - a real rate limit belongs at the reverse
proxy or CDN, which is also the only layer that can see a true client
address rather than whatever a client chose to put in `X-Forwarded-For`.

This app therefore reads `request.client.host` and nothing else. Trusting
a forwarding header without knowing how many proxies sit in front makes
the limit trivially bypassable by spoofing one, which is strictly worse
than a limit that merely coalesces everyone behind one proxy into a single
bucket - the failure mode of the honest version is over-throttling, and
the failure mode of the dishonest one is no throttling at all.

No new dependency backs this, and no Redis: the same reasoning D049 gave
for the login lockout applies unchanged (Redis is provisioned in
docker-compose.yml but still unwired, and wiring it for this alone would
add a hard runtime dependency to the auth path).

The per-ACCOUNT limit is a separate, stronger mechanism and lives in the
route: it counts real `password_reset_tokens` rows, so it holds across
workers and restarts. This module bounds requests for emails that do not
exist, which the row count by construction cannot see.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

WINDOW = timedelta(hours=1)
"""One hour, matching the per-account limit's window so an operator has one
number to reason about rather than two."""

MAX_TRACKED_KEYS = 10_000
"""Hard cap on distinct keys held at once. Without it, an attacker cycling
source addresses turns a throttle into an unbounded memory leak - the
mitigation becoming the vulnerability. On overflow the least-recently-seen
key is evicted, which loses that key's history; that is the correct
trade-off, because forgetting an old bucket only ever under-throttles,
never blocks a legitimate caller."""


@dataclass
class FixedWindowThrottle:
    """Sliding-window request counter keyed by an arbitrary string.

    Deliberately takes `now` from the caller on every call rather than
    reading a clock itself: the route already has one clock seam
    (`password_reset.utcnow`), and two independent clocks on the same path
    is how tests start disagreeing with production.
    """

    _hits: dict[str, deque[datetime]] = field(default_factory=dict)

    def allow(self, key: str, now: datetime, *, limit: int) -> bool:
        """True if this request is within `limit` for `key` in the last
        hour, and records it. `limit <= 0` disables the throttle entirely
        (documented in Settings as the way to turn it off), in which case
        nothing is recorded either - a disabled throttle should not quietly
        accumulate state."""
        if limit <= 0:
            return True

        cutoff = now - WINDOW
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= MAX_TRACKED_KEYS:
                self._evict_oldest()
            hits = deque()
            self._hits[key] = hits

        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= limit:
            # Deliberately NOT recorded. Counting refused attempts would
            # make one burst extend the block indefinitely, which is a
            # denial-of-service against whoever shares that address.
            return False

        hits.append(now)
        # Re-insert so `_evict_oldest` (which relies on dict insertion
        # order) treats this key as the most recently seen.
        self._hits[key] = self._hits.pop(key)
        return True

    def _evict_oldest(self) -> None:
        oldest = next(iter(self._hits))
        del self._hits[oldest]

    def reset(self) -> None:
        """Tests only. Never called by the application - a running server
        has no legitimate reason to forget its throttle state."""
        self._hits.clear()


reset_request_throttle = FixedWindowThrottle()
"""Module-level singleton, which is what makes it per-process. Importing
the module twice under different names would give two independent
throttles; there is exactly one import site (the reset route)."""
