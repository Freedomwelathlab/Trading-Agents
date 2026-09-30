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

Phase 106 (D127): **a display name is resolved to a model id.** The
operator set `LLM_PROVIDER_MODEL=Claude Fable 5` - the name a console shows
- and Anthropic answered 404 `not_found_error`, because the API wants an id
such as `claude-fable-5-1`. On a model 404 the provider now asks the
endpoint's own `GET /v1/models` which ids exist and picks the one the
configured text names: an exact id, an exact display name, or the newest id
that starts with the name written as a slug (`claude-fable-5` ->
`claude-fable-5-1`). It retries once with that id and remembers it for the
process. It never falls back to a different model family: when nothing
matches, the error lists the ids that do exist so the variable can be set.
"""

import re
from typing import Any

import httpx

from apps.api.app.agents.provider import LLMProviderError
from apps.api.app.core.config import Settings

DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

# (base_url, configured model text) -> the model id it resolved to. Process
# lifetime is enough: a redeploy with a corrected variable starts fresh.
_RESOLVED: dict[tuple[str, str], str] = {}


def model_slug(name: str) -> str:
    """`Claude Fable 5.1` -> `claude-fable-5-1`; an id passes through."""
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def pick_model(configured: str, models: list[dict[str, Any]]) -> str | None:
    """The listed model the configured text names, or None.

    Order: exact id, exact display name (case-insensitive), then the newest
    id that equals or starts with the slug plus a hyphen. Never a model the
    text does not name."""
    ids = [str(m.get("id")) for m in models if m.get("id")]
    if configured in ids:
        return configured
    wanted = configured.strip().lower()
    for m in models:
        if str(m.get("display_name", "")).strip().lower() == wanted and m.get("id"):
            return str(m["id"])
    slug = model_slug(configured)
    if slug in ids:
        return slug
    prefixed = [m for m in models if str(m.get("id", "")).startswith(slug + "-")]
    if not prefixed:
        return None
    prefixed.sort(key=lambda m: (str(m.get("created_at", "")), str(m.get("id"))), reverse=True)
    return str(prefixed[0]["id"])


def _is_model_not_found(response: httpx.Response) -> bool:
    if response.status_code != 404:
        return False
    text = response.text.lower()
    return "not_found" in text and "model" in text


class AnthropicCompatibleProvider:
    name = "anthropic-compatible"

    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._configured = model.strip()
        self._model = _RESOLVED.get((self._base_url, self._configured), self._configured)

    def _headers(self) -> dict[str, str]:
        return {
            # Both auth schemes, deliberately: Anthropic reads `x-api-key`,
            # gateways generally read the bearer, and neither objects to the
            # other being present.
            "x-api-key": self._api_key,
            "Authorization": f"Bearer {self._api_key}",
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    async def _list_models(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        models: list[dict[str, Any]] = []
        params: dict[str, Any] = {"limit": 100}
        for _ in range(5):  # a handful of pages is every model there is
            response = await client.get(
                f"{self._base_url}/v1/models", params=params, headers=self._headers()
            )
            response.raise_for_status()
            body = response.json()
            models.extend(m for m in body.get("data", []) if isinstance(m, dict))
            if not body.get("has_more") or not body.get("last_id"):
                break
            params["after_id"] = body["last_id"]
        return models

    async def _resolve_model(self, client: httpx.AsyncClient) -> str:
        """D127: the id the configured model text names, from the endpoint's
        own model list. Raises with the available ids when nothing matches."""
        try:
            models = await self._list_models(client)
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMProviderError(
                f"LLM provider does not know the model {self._configured!r} (HTTP 404), and "
                f"its model list could not be read to resolve it ({exc}). Set "
                "LLM_PROVIDER_MODEL to an exact model id."
            ) from exc
        picked = pick_model(self._configured, models)
        if picked is None:
            available = ", ".join(sorted(str(m.get("id")) for m in models)[:15]) or "none listed"
            raise LLMProviderError(
                f"LLM provider does not know the model {self._configured!r}. "
                f"Set LLM_PROVIDER_MODEL to one of: {available}."
            )
        _RESOLVED[(self._base_url, self._configured)] = picked
        return picked

    async def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await self._post(client, system=system, user=user, max_tokens=max_tokens)
                if _is_model_not_found(response) and self._model == self._configured:
                    resolved = await self._resolve_model(client)
                    if resolved != self._model:
                        self._model = resolved
                        response = await self._post(
                            client, system=system, user=user, max_tokens=max_tokens
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

    async def _post(
        self, client: httpx.AsyncClient, *, system: str, user: str, max_tokens: int
    ) -> httpx.Response:
        payload = {
            "model": self._model,
            "system": system,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user}],
        }
        return await client.post(
            f"{self._base_url}/v1/messages", json=payload, headers=self._headers()
        )


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
