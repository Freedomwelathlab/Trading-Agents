"""Request correlation IDs (Phase 42, docs/DECISIONS.md D056).

These are integration tests through the real app object and the real
middleware stack, not unit tests against the resolver alone — the thing
worth proving is that a response actually carries the header and that a log
line emitted deep inside request handling actually carries the ID, neither
of which a pure unit test of `resolve_request_id()` would establish.
"""

import json
import uuid

import structlog
from fastapi.testclient import TestClient

from apps.api.app.core.logging import _SECRET_KEY_PATTERN, configure_logging, get_logger
from apps.api.app.core.request_id import (
    REQUEST_ID_HEADER,
    REQUEST_ID_LOG_KEY,
    is_valid_request_id,
    resolve_request_id,
)
from apps.api.app.main import app

client = TestClient(app)


def test_every_response_carries_a_request_id_header():
    response = client.get("/health")
    assert response.status_code == 200
    assert is_valid_request_id(response.headers[REQUEST_ID_HEADER])


def test_two_requests_get_two_different_ids():
    first = client.get("/health").headers[REQUEST_ID_HEADER]
    second = client.get("/health").headers[REQUEST_ID_HEADER]
    assert first != second
    # Both are the generated form (UUID4), since neither call supplied one.
    uuid.UUID(first)
    uuid.UUID(second)


def test_a_well_formed_caller_supplied_id_is_honored():
    supplied = "edge-lb-01.7f3c9a12"
    response = client.get("/health", headers={REQUEST_ID_HEADER: supplied})
    assert response.headers[REQUEST_ID_HEADER] == supplied


def test_a_supplied_uuid_is_honored_unchanged():
    supplied = str(uuid.uuid4())
    response = client.get("/health", headers={REQUEST_ID_HEADER: supplied})
    assert response.headers[REQUEST_ID_HEADER] == supplied


def test_the_header_is_returned_on_error_responses_too():
    """A correlation ID is most useful on the requests that failed."""
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    assert is_valid_request_id(response.headers[REQUEST_ID_HEADER])


def test_the_header_is_returned_on_401s():
    response = client.get("/admin/users")
    assert response.status_code == 401
    assert is_valid_request_id(response.headers[REQUEST_ID_HEADER])


class TestMalformedSuppliedIDsAreReplacedNotRejected:
    """Documented contract: a bad `X-Request-ID` never fails the request.

    The value is a correlation label, not an authorization input, so a
    misconfigured proxy must not be able to turn a real trading request
    into a 4xx. It is replaced with a fresh UUID4 instead.
    """

    def test_too_short_is_replaced(self):
        response = client.get("/health", headers={REQUEST_ID_HEADER: "abc"})
        assert response.status_code == 200
        returned = response.headers[REQUEST_ID_HEADER]
        assert returned != "abc"
        uuid.UUID(returned)

    def test_too_long_is_replaced(self):
        supplied = "a" * 129
        response = client.get("/health", headers={REQUEST_ID_HEADER: supplied})
        assert response.status_code == 200
        assert response.headers[REQUEST_ID_HEADER] != supplied
        uuid.UUID(response.headers[REQUEST_ID_HEADER])

    def test_a_header_injection_attempt_is_replaced(self):
        """The charset exists to foreclose CR/LF smuggling into responses."""
        assert not is_valid_request_id("abcdefgh\r\nX-Evil: yes")
        assert not is_valid_request_id("has spaces in it")
        assert not is_valid_request_id("semi;colon;value")

    def test_an_empty_header_is_replaced(self):
        response = client.get("/health", headers={REQUEST_ID_HEADER: ""})
        assert response.status_code == 200
        uuid.UUID(response.headers[REQUEST_ID_HEADER])


def test_resolver_reports_whether_the_supplied_value_was_honored():
    honored_id, honored = resolve_request_id("client-supplied-0001")
    assert (honored_id, honored) == ("client-supplied-0001", True)

    generated_id, honored = resolve_request_id("bad!")
    assert honored is False
    uuid.UUID(generated_id)

    absent_id, honored = resolve_request_id(None)
    assert honored is False
    uuid.UUID(absent_id)


def test_the_request_id_log_key_does_not_collide_with_secret_redaction():
    """If `request_id` ever matched the secret pattern from D-secrets, every
    correlation ID would reach the sink as `***REDACTED***` and this whole
    mechanism would be silently useless. Guard the collision explicitly."""
    assert _SECRET_KEY_PATTERN.search(REQUEST_ID_LOG_KEY) is None


def test_the_id_reaches_every_log_line_of_one_request(capsys):
    """The actual point of the feature: several independent log calls made
    while handling one request all carry the same ID, without any of them
    passing it explicitly — and the secret redactor still runs on them.

    Read from stdout rather than `caplog`: `configure_logging()` leaves
    structlog on its default `PrintLoggerFactory`, so rendered JSON goes to
    stdout and never becomes a stdlib `LogRecord` (`caplog.records` is
    empty here — verified, not assumed).
    """
    configure_logging("INFO")
    logger = get_logger("test_request_id")

    @app.get("/_test_multi_log_lines")
    async def _multi_log_lines() -> dict[str, str]:
        logger.info("risk_decision", verdict="approved")
        logger.info("portfolio_decision", verdict="sized")
        secret = "sk-live-should-not-appear"  # pragma: allowlist secret
        logger.info("fill_recorded", api_key=secret)
        return {"ok": "true"}

    try:
        supplied = "phase42-correlation-check"
        response = client.get("/_test_multi_log_lines", headers={REQUEST_ID_HEADER: supplied})
        assert response.status_code == 200
        assert response.headers[REQUEST_ID_HEADER] == supplied

        events = {}
        for line in capsys.readouterr().out.splitlines():
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("event") in {
                "risk_decision",
                "portfolio_decision",
                "fill_recorded",
            }:
                events[payload["event"]] = payload

        assert set(events) == {"risk_decision", "portfolio_decision", "fill_recorded"}
        for payload in events.values():
            assert payload[REQUEST_ID_LOG_KEY] == supplied
        # The redaction processor still runs alongside the new binding.
        assert events["fill_recorded"]["api_key"] == "***REDACTED***"
    finally:
        app.router.routes = [
            route
            for route in app.router.routes
            if getattr(route, "path", None) != "/_test_multi_log_lines"
        ]


def test_the_context_is_unbound_after_the_request():
    """Uvicorn reuses a task context across requests on one connection, so a
    leaked binding would stamp the previous request's ID onto the next
    one's logs — and onto background work that has no request at all."""
    structlog.contextvars.clear_contextvars()
    client.get("/health")
    assert REQUEST_ID_LOG_KEY not in structlog.contextvars.get_contextvars()
