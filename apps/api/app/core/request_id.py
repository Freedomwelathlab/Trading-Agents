"""Per-request correlation IDs (Phase 42, docs/DECISIONS.md D056).

THE PROBLEM
-----------
Every log line this service emits is structured JSON, but until now none of
them carried anything tying a line back to the specific HTTP request that
produced it. One trade submission can emit a risk decision, a portfolio
decision, and a fill on three separate lines; in a production log stream
interleaved across concurrent requests and workers there was no way to
group those three back into "this one request". That makes the single most
common production question — "the user says their 14:03 order was rejected,
what happened?" — unanswerable from the logs alone.

WHAT THIS DOES
--------------
`RequestIDMiddleware` assigns every incoming HTTP request an ID, binds it
into structlog's contextvars context for the duration of that request, and
echoes it back on the response as `X-Request-ID`. Because
`structlog.contextvars.merge_contextvars` is already the first processor in
`configure_logging()` (apps/api/app/core/logging.py), every
`logger.info(...)` emitted anywhere under that request picks the ID up
automatically — no call site had to change, and none did.

WHY PURE ASGI AND NOT `BaseHTTPMiddleware`
------------------------------------------
`BaseHTTPMiddleware` runs the downstream app in a child anyio task. A
context variable bound in `dispatch()` before `call_next()` does reach that
child (the task copies the context at spawn), but anything the child binds
is invisible to the parent, and the reset half of the lifecycle then
straddles two contexts. A pure ASGI middleware runs the whole request in
one context, so bind/reset are exactly paired, and it can rewrite the
response headers from the `http.response.start` message directly. No new
dependency is involved either way — this is Starlette's own protocol.

TRUSTING A CALLER-SUPPLIED ID
-----------------------------
An inbound `X-Request-ID` is honored when it is well-formed, so a load
balancer, an ingress, or the Next.js frontend can propagate one ID across a
hop and have both sides' logs join up. "Well-formed" is deliberately
narrow: 8-128 characters drawn from `[A-Za-z0-9._-]` only. Anything else
(too short, too long, or containing whitespace, control characters, CR/LF,
or any other byte) is **discarded and replaced with a fresh UUID4** rather
than rejected with a 4xx.

Replace-don't-reject is the defensible choice here because this value is a
correlation label and nothing else. It grants no access, gates no code
path, and is never compared against anything; failing a real trading
request because a proxy sent an oddly-shaped header would turn a cosmetic
observability concern into an outage. The narrow charset is what makes it
safe to echo back in a response header at all — it is precisely what
forecloses header injection via CR/LF, and the length bound keeps a hostile
caller from writing unbounded attacker-controlled text into every log line
of the request. When a supplied ID is rejected the substitution is logged
once, so a misconfigured upstream is visible rather than silent.

The ID is bound under the log key `request_id`. That name is checked
against `_SECRET_KEY_PATTERN` in `core/logging.py` by
`tests/core/test_request_id.py` — it must never collide with the secret
redaction patterns (`secret|password|passwd|api_key|token|private_key|
access_key`), or the correlation ID would arrive at the sink as
`***REDACTED***` and this whole mechanism would be silently useless.
"""

import re
import uuid
from typing import Any

import structlog

from apps.api.app.core.logging import get_logger

logger = get_logger(__name__)

#: The HTTP header carried in both directions.
REQUEST_ID_HEADER = "X-Request-ID"

#: The structlog key the ID is bound under. Deliberately not matched by
#: `core.logging._SECRET_KEY_PATTERN`.
REQUEST_ID_LOG_KEY = "request_id"

_MIN_LENGTH = 8
_MAX_LENGTH = 128
_VALID_REQUEST_ID = re.compile(rf"^[A-Za-z0-9._-]{{{_MIN_LENGTH},{_MAX_LENGTH}}}$")


def is_valid_request_id(value: str) -> bool:
    """Whether a caller-supplied ID may be reused as-is.

    See this module's docstring for why the charset is this narrow: the
    value is echoed into a response header and into every log line of the
    request, so CR/LF and unbounded length are the two things that must not
    get through.
    """
    return bool(_VALID_REQUEST_ID.match(value))


def generate_request_id() -> str:
    return str(uuid.uuid4())


def resolve_request_id(supplied: str | None) -> tuple[str, bool]:
    """Return `(request_id, was_supplied_and_honored)`.

    A malformed or absent header yields a fresh UUID4 rather than an error
    — this is a correlation label, never an authorization input.
    """
    if supplied is not None and is_valid_request_id(supplied):
        return supplied, True
    return generate_request_id(), False


class RequestIDMiddleware:
    """Pure-ASGI middleware binding a correlation ID for each HTTP request."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # `lifespan` and `websocket` scopes have no request/response header
        # cycle to attach an ID to; pass them straight through untouched.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        supplied = _header_value(scope.get("headers") or [], REQUEST_ID_HEADER)
        request_id, honored = resolve_request_id(supplied)

        if supplied is not None and not honored:
            # Bound first so this line itself carries the substitute ID.
            structlog.contextvars.bind_contextvars(**{REQUEST_ID_LOG_KEY: request_id})
            logger.warning(
                "request_id_header_rejected",
                # The offending value is NOT echoed: it is unvalidated
                # caller-controlled text, and repeating it into the log
                # stream is the injection this rejection just prevented.
                supplied_length=len(supplied),
                reason="malformed",
            )
        else:
            structlog.contextvars.bind_contextvars(**{REQUEST_ID_LOG_KEY: request_id})

        # Recorded on the scope so route handlers and exception handlers can
        # read the ID without re-deriving it from headers.
        scope["request_id"] = request_id

        async def send_with_request_id(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers = [
                    (name, value)
                    for (name, value) in headers
                    if name.decode("latin-1").lower() != REQUEST_ID_HEADER.lower()
                ]
                headers.append(
                    (REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1"))
                )
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # Unbind rather than reset-by-token: uvicorn reuses the same
            # task context across requests on a connection, so leaving the
            # key bound would leak one request's ID into the next one's
            # logs.
            structlog.contextvars.unbind_contextvars(REQUEST_ID_LOG_KEY)


def _header_value(raw_headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for key, value in raw_headers:
        if key.lower() == wanted:
            try:
                return value.decode("latin-1")
            except UnicodeDecodeError:  # pragma: no cover - latin-1 cannot fail
                return None
    return None
