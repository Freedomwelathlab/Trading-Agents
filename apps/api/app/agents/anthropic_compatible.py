"""Concrete LLMProvider talking to any Anthropic Messages-API-compatible
endpoint (POST {base_url}/v1/messages, bearer auth). OmniRoute is the
first (and, today, only) endpoint actually configured against this - see
docs/DECISIONS.md D018 for why the client stays provider-name-agnostic
rather than hard-coding anything OmniRoute-specific.
"""

from typing import Any

import httpx

from apps.api.app.agents.provider import LLMProviderError
from apps.api.app.core.config import Settings


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
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self._base_url}/v1/messages", json=payload, headers=headers
                )
                response.raise_for_status()
                body: Any = response.json()
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
    if not (
        settings.llm_provider_base_url
        and settings.llm_provider_api_key
        and settings.llm_provider_model
    ):
        return None
    return AnthropicCompatibleProvider(
        base_url=settings.llm_provider_base_url,
        api_key=settings.llm_provider_api_key,
        model=settings.llm_provider_model,
    )
