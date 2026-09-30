"""The bridge agent's server side (Phase 104, D124).

IBKR and moomoo are reachable only through a gateway on the operator's own
PC. A small agent there pulls jobs from this package's queue over an
outbound connection and posts the gateway's answers back; nothing on the
PC listens for inbound traffic. See `docs/DECISIONS.md` D124.
"""
