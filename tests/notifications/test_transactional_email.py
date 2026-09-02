"""Tests for the transactional-email adapter (docs/DECISIONS.md D062).

The HTTP layer is exercised through `httpx.MockTransport`, so the real
client code - URL joining, header construction, body shape, status handling
- runs unmodified against a scripted server. Nothing here reaches the
network, and no real vendor credential exists anywhere in this file.

The contract asserted below is the one documented in
apps/api/app/notifications/transactional_email.py's module docstring. If a
future vendor needs a different shape, it implements the EmailProvider
Protocol as a second adapter; it does not loosen these assertions.
"""

import json

import httpx
import pytest

from apps.api.app.core.config import Settings
from apps.api.app.notifications import transactional_email
from apps.api.app.notifications.provider import EmailProviderError
from apps.api.app.notifications.transactional_email import (
    HttpTransactionalEmailProvider,
    build_email_provider,
)


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
        **overrides,
    )


def _configured(**overrides) -> Settings:
    trio = {
        "email_provider_base_url": "https://mail.test/api",
        "email_provider_api_key": "fake-email-key-for-tests",  # pragma: allowlist secret
        "email_provider_from_address": "ops@trading-os.test",
    }
    return _settings(**{**trio, **overrides})


_REAL_ASYNC_CLIENT = httpx.AsyncClient
"""Captured before any patching. The factory below must construct a REAL
client, not the patched name, or it recurses into itself."""


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Point the adapter's client at a scripted transport, leaving every
    other line of `send()` - URL joining, headers, body, status handling -
    running exactly as it does in production. `monkeypatch` restores the
    real class afterwards, so this cannot leak into the ASGI test clients
    the API suites build from the same class."""

    def factory(**kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(transactional_email.httpx, "AsyncClient", factory)


def test_no_provider_is_built_when_nothing_is_configured(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "EMAIL_PROVIDER_BASE_URL",
        "EMAIL_PROVIDER_API_KEY",
        "EMAIL_PROVIDER_FROM_ADDRESS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert build_email_provider(_settings()) is None


@pytest.mark.parametrize(
    "missing",
    ["email_provider_base_url", "email_provider_api_key", "email_provider_from_address"],
)
def test_a_partial_credential_set_builds_nothing(
    missing: str, monkeypatch: pytest.MonkeyPatch
):
    """All-or-nothing, exactly like the LONGPORT_* and LLM_PROVIDER_* trios
    (D008/D015/D018). Two of three values is a misconfiguration, and the
    honest response to it is NOT_CONFIGURED rather than a half-built client
    that fails on the first send."""
    for name in (
        "EMAIL_PROVIDER_BASE_URL",
        "EMAIL_PROVIDER_API_KEY",
        "EMAIL_PROVIDER_FROM_ADDRESS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert build_email_provider(_configured(**{missing: None})) is None


def test_a_complete_credential_set_builds_a_provider(monkeypatch: pytest.MonkeyPatch):
    provider = build_email_provider(_configured())
    assert isinstance(provider, HttpTransactionalEmailProvider)
    assert provider.name == "http-transactional-email"


@pytest.mark.asyncio
async def test_send_posts_the_documented_contract(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"id": "msg-1"})

    _install_transport(monkeypatch, handler)
    provider = HttpTransactionalEmailProvider(
        base_url="https://mail.test/api/",  # trailing slash must not double up
        api_key="fake-email-key-for-tests",  # pragma: allowlist secret
        from_address="ops@trading-os.test",
    )
    await provider.send(to="user@example.com", subject="Subject line", text="Body text")

    assert seen["method"] == "POST"
    assert seen["url"] == "https://mail.test/api/emails"
    assert seen["auth"] == "Bearer fake-email-key-for-tests"
    body = json.loads(str(seen["body"]))
    # `to` is a LIST even for one recipient - a bare string is silently
    # misread by several vendors that otherwise share this shape.
    assert body == {
        "from": "ops@trading-os.test",
        "to": ["user@example.com"],
        "subject": "Subject line",
        "text": "Body text",
    }


@pytest.mark.asyncio
async def test_a_2xx_other_than_200_still_counts_as_accepted(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_transport(monkeypatch, lambda request: httpx.Response(202, json={"id": "q"}))
    provider = HttpTransactionalEmailProvider(
        base_url="https://mail.test/api",
        api_key="fake-email-key-for-tests",  # pragma: allowlist secret
        from_address="ops@trading-os.test",
    )
    await provider.send(to="user@example.com", subject="s", text="t")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 422, 429, 500, 503])
async def test_a_non_2xx_raises_rather_than_reporting_a_send(
    status: int, monkeypatch: pytest.MonkeyPatch
):
    """The single most important property of this adapter: returning
    normally is a claim that a message was accepted, so it must not be made
    on a rejection."""
    _install_transport(monkeypatch, lambda request: httpx.Response(status, text="nope"))
    provider = HttpTransactionalEmailProvider(
        base_url="https://mail.test/api",
        api_key="fake-email-key-for-tests",  # pragma: allowlist secret
        from_address="ops@trading-os.test",
    )
    with pytest.raises(EmailProviderError):
        await provider.send(to="user@example.com", subject="s", text="t")


@pytest.mark.asyncio
async def test_a_transport_failure_raises_the_typed_error(
    monkeypatch: pytest.MonkeyPatch,
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    _install_transport(monkeypatch, handler)
    provider = HttpTransactionalEmailProvider(
        base_url="https://mail.test/api",
        api_key="fake-email-key-for-tests",  # pragma: allowlist secret
        from_address="ops@trading-os.test",
    )
    with pytest.raises(EmailProviderError, match="Email provider request failed"):
        await provider.send(to="user@example.com", subject="s", text="t")


@pytest.mark.asyncio
async def test_the_raised_error_never_carries_the_api_key(
    monkeypatch: pytest.MonkeyPatch,
):
    """Spec section 38: a credential must not travel in an error message that
    could reach a log sink or an HTTP response body (the admin endpoint
    echoes this error's text)."""
    _install_transport(monkeypatch, lambda request: httpx.Response(401, text="bad key"))
    provider = HttpTransactionalEmailProvider(
        base_url="https://mail.test/api",
        api_key="super-secret-value-abc123",  # pragma: allowlist secret
        from_address="ops@trading-os.test",
    )
    with pytest.raises(EmailProviderError) as caught:
        await provider.send(to="user@example.com", subject="s", text="t")
    assert "super-secret-value-abc123" not in str(caught.value)
