"""The Messages-API client (D018, extended in Phase 86 / D104).

Two of these assert things that were silently wrong before Phase 86 and
that no amount of local testing against a gateway would have caught: the
auth header Anthropic's own API requires, and the version header it
rejects a request without. The rest pin the refusal behaviour — a provider
that is not configured must be `None`, and a provider error must name the
status, because "request failed" makes a wrong API key look like an
outage.
"""

import httpx
import pytest

from apps.api.app.agents import anthropic_compatible as mod
from apps.api.app.agents.anthropic_compatible import (
    ANTHROPIC_VERSION,
    DEFAULT_ANTHROPIC_BASE_URL,
    AnthropicCompatibleProvider,
    build_llm_provider,
)
from apps.api.app.agents.provider import LLMProviderError
from apps.api.app.core.config import Settings

_REAL_CLIENT = httpx.AsyncClient


def _wire(monkeypatch, handler):
    """Route the provider's own client at an in-memory transport."""
    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return _REAL_CLIENT(transport=transport, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)


def _settings(**over) -> Settings:
    # Every field is pinned explicitly, including the ones being defaulted:
    # `Settings` reads the developer's own `.env`, so a helper that left
    # `llm_provider_base_url` unset would assert against whatever gateway
    # happens to be configured on this machine rather than against the code.
    base = dict(
        llm_provider_base_url=None,
        llm_provider_api_key="k",
        llm_provider_model="m",
    )
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_sends_both_auth_headers_and_the_anthropic_version(monkeypatch):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "hi"}]})

    _wire(monkeypatch, handler)
    provider = AnthropicCompatibleProvider(base_url="https://x", api_key="secret", model="m")
    assert await provider.complete(system="s", user="u", max_tokens=16) == "hi"

    # Anthropic reads x-api-key; gateways read the bearer. Sending one only
    # 401s against the other, which is exactly the bug this fixes.
    assert seen["x-api-key"] == "secret"
    assert seen["authorization"] == "Bearer secret"
    assert seen["anthropic-version"] == ANTHROPIC_VERSION


@pytest.mark.asyncio
async def test_an_http_error_names_the_status_and_the_providers_own_message(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid x-api-key"}})

    _wire(monkeypatch, handler)
    provider = AnthropicCompatibleProvider(base_url="https://x", api_key="bad", model="m")
    with pytest.raises(LLMProviderError) as err:
        await provider.complete(system="s", user="u", max_tokens=16)
    assert "401" in str(err.value)
    assert "invalid x-api-key" in str(err.value)
    # The key travels in a request header and must never come back out in
    # an error a UI will render.
    assert "bad" not in str(err.value).replace("invalid x-api-key", "")


@pytest.mark.asyncio
async def test_a_response_with_no_text_block_is_an_error_not_an_empty_answer(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "tool_use"}]})

    _wire(monkeypatch, handler)
    provider = AnthropicCompatibleProvider(base_url="https://x", api_key="k", model="m")
    with pytest.raises(LLMProviderError):
        await provider.complete(system="s", user="u", max_tokens=16)


def test_a_key_and_a_model_are_enough_and_default_to_anthropics_own_api():
    provider = build_llm_provider(_settings())
    assert provider is not None
    assert provider._base_url == DEFAULT_ANTHROPIC_BASE_URL


def test_an_explicit_base_url_still_wins():
    provider = build_llm_provider(_settings(llm_provider_base_url="http://127.0.0.1:20128/api/v1/"))
    assert provider is not None
    # The trailing slash is stripped so the joined path is not `//v1/messages`.
    assert provider._base_url == "http://127.0.0.1:20128/api/v1"


@pytest.mark.parametrize(
    "over",
    [
        {"llm_provider_api_key": None},
        {"llm_provider_model": None},
    ],
)
def test_a_half_configured_provider_is_none_not_a_guessed_default(over):
    # NOT_CONFIGURED is the honest answer; guessing a model for a key (or a
    # key for a model) would make a misconfiguration look like a working
    # agent right up until it billed someone.
    assert build_llm_provider(_settings(**over)) is None


# --- Phase 106 (D127): a display name resolves to a model id ---------------

_MODELS = {
    "data": [
        {"id": "claude-fable-5-1", "display_name": "Claude Fable 5.1", "created_at": "2026-08-01"},
        {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5", "created_at": "2026-07-01"},
    ],
    "has_more": False,
}


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("claude-opus-5-5", "claude-opus-5-5"),  # exact id
        ("Claude Opus 5.5", "claude-opus-5-5"),  # exact display name
        ("Claude Fable 5", "claude-fable-5-1"),  # slug prefix
        ("claude fable 5.1", "claude-fable-5-1"),
        ("Claude Haiku 9", None),  # never another family
    ],
)
def test_pick_model(configured, expected):
    assert mod.pick_model(configured, _MODELS["data"]) == expected


@pytest.mark.asyncio
async def test_a_display_name_is_resolved_and_retried_once(monkeypatch):
    mod._RESOLVED.clear()
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=_MODELS)
        model = __import__("json").loads(request.content)["model"]
        sent.append(model)
        if model != "claude-fable-5-1":
            return httpx.Response(
                404,
                json={
                    "type": "error",
                    "error": {"type": "not_found_error", "message": f"model: {model}"},
                },
            )
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    _wire(monkeypatch, handler)
    provider = AnthropicCompatibleProvider(
        base_url="https://x", api_key="k", model="Claude Fable 5"
    )
    assert await provider.complete(system="s", user="u", max_tokens=5) == "ok"
    assert sent == ["Claude Fable 5", "claude-fable-5-1"]
    # A new provider in the same process goes straight to the resolved id.
    again = AnthropicCompatibleProvider(base_url="https://x", api_key="k", model="Claude Fable 5")
    assert await again.complete(system="s", user="u", max_tokens=5) == "ok"
    assert sent[-1] == "claude-fable-5-1" and len(sent) == 3
    mod._RESOLVED.clear()


@pytest.mark.asyncio
async def test_an_unknown_model_lists_the_ids_that_exist(monkeypatch):
    mod._RESOLVED.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=_MODELS)
        return httpx.Response(
            404, json={"type": "error", "error": {"type": "not_found_error", "message": "model: x"}}
        )

    _wire(monkeypatch, handler)
    provider = AnthropicCompatibleProvider(
        base_url="https://x", api_key="k", model="Claude Haiku 9"
    )
    with pytest.raises(LLMProviderError) as err:
        await provider.complete(system="s", user="u", max_tokens=5)
    assert "LLM_PROVIDER_MODEL" in str(err.value) and "claude-fable-5-1" in str(err.value)
