"""Concrete LLMProvider talking to any Anthropic Messages-API-compatible
endpoint (POST {base_url}/v1/messages). See docs/DECISIONS.md D018 for why
the client stays provider-name-agnostic rather than hard-coding anything
one gateway needs.

Phase 86 (D104) made it work against Anthropic's own API as well as a
gateway. Two things had to change and both were silent failures before:

* **Auth header.** `api.anthropic.com` authenticates with `x-api-key`;
  most gateways in front of it take `Authorization: Bearer`. Sending only
  the bearer produced a 401 from Anthropic that surfaced in the UI as the
  same opaque "request failed" a network outage gives. Both headers are
  now sent — each endpoint reads the one it knows and ignores the other.
* **`anthropic-version`.** Anthropic requires it and rejects a request
  without it; gateways ignore it.

`DEFAULT_ANTHROPIC_BASE_URL` also means an operator who has an Anthropic
key only has to set the key and the model. It is a documented default for
a published API, not a fallback that invents a provider: with no key at
all `build_llm_provider` still returns None and every caller still renders
NOT_CONFIGURED.
"""

from typing import Any

import httpx

from apps.api.app.agents.provider import LLMProviderError
from apps.api.app.core.config import Settings

DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicCompatibleProvider:
    name = "anthropic-compatible"

    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    async def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        payload = {
            "model": self._model,
            "system": system,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            # Both auth schemes, deliberately: Anthropic reads `x-api-key`,
            # gateways generally read the bearer, and neither objects to the
            # other being present.
            "x-api-key": self._api_key,
            "Authorization": f"Bearer {self._api_key}",
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self._base_url}/v1/messages", json=payload, headers=headers
                )
                response.raise_for_status()
                body: Any = response.json()
        except httpx.HTTPStatusError as exc:
            # The status and the provider's own error text are the whole
            # diagnosis for the two failures an operator actually hits — a
            # wrong key (401) and a wrong model name (404/400) — and a bare
            # "request failed" makes them indistinguishable from an outage.
            # The response body never contains the key; the key is only ever
            # in the request headers, which are not echoed here.
            detail = exc.response.text[:400] if exc.response is not None else ""
            raise LLMProviderError(
                f"LLM provider returned HTTP {exc.response.status_code}: {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"LLM provider request failed: {exc}") from exc

        try:
            blocks = body["content"]
            text = "".join(block["text"] for block in blocks if block.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise LLMProviderError(
                f"LLM provider returned an unexpected response shape: {body!r}"
            ) from exc

        if not text:
            raise LLMProviderError("LLM provider returned no text content.")
        return text


def build_llm_provider(settings: Settings) -> AnthropicCompatibleProvider | None:
    """None means no provider is configured (D018) - callers must render
    that as NOT_CONFIGURED, never silently skip the check or fall back to
    a default model/key."""
    if not (settings.llm_provider_api_key and settings.llm_provider_model):
        return None
    return AnthropicCompatibleProvider(
        base_url=settings.llm_provider_base_url or DEFAULT_ANTHROPIC_BASE_URL,
        api_key=settings.llm_provider_api_key,
        model=settings.llm_provider_model,
    )
