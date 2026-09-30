"""Trading OS bridge agent (Phase 104, D124).

Runs on the operator's own Windows PC, next to the IBKR Client Portal
Gateway and/or moomoo OpenD. It opens every connection itself - long-polls
the platform for jobs, runs them against the local gateways, posts the
answers back - and listens on nothing. See README.md.

Standalone on purpose: it imports nothing from `apps/`, so it can be
copied to a PC with only Python and `httpx` installed.
"""

VERSION = "1.0.0"
