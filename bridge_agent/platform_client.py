"""The agent's side of `/bridge/agent/*`. Every connection is outbound."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from bridge_agent import VERSION

log = logging.getLogger("bridge_agent")


class PlatformError(Exception):
    """The platform refused or could not be reached."""


class PlatformClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        agent_id: str,
        claim_wait_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._claim_wait = claim_wait_seconds
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": f"trading-os-bridge-agent/{VERSION}",
            },
            # The claim is a long-poll: allow it to run its full wait.
            timeout=httpx.Timeout(claim_wait_seconds + 20.0, connect=10.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        try:
            response = self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise PlatformError(f"Platform unreachable: {type(exc).__name__}: {exc}") from exc
        if response.status_code == 401:
            raise PlatformError("The platform rejected BRIDGE_AGENT_TOKEN (401).")
        if response.status_code == 503:
            raise PlatformError(f"The platform's bridge is disabled: {response.text[:300]}")
        return response

    def claim(self, providers: tuple[str, ...]) -> dict[str, Any] | None:
        response = self._post(
            "/bridge/agent/claim",
            {
                "agent_id": self._agent_id,
                "providers": list(providers),
                "wait_seconds": self._claim_wait,
            },
        )
        if response.status_code != 200:
            raise PlatformError(f"Claim failed: HTTP {response.status_code} {response.text[:300]}")
        job = response.json().get("job")
        return job if isinstance(job, dict) else None

    def post_result(
        self,
        job_id: str,
        *,
        ok: bool,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> str:
        response = self._post(
            f"/bridge/agent/jobs/{job_id}/result",
            {"agent_id": self._agent_id, "ok": ok, "result": result, "error": error},
        )
        if response.status_code == 200:
            return "recorded"
        if response.status_code == 409:
            # Late or duplicate. The platform still recorded a late answer.
            log.warning("job %s: platform answered 409: %s", job_id, response.text[:300])
            return "late"
        raise PlatformError(
            f"Posting job {job_id} failed: HTTP {response.status_code} {response.text[:300]}"
        )

    def heartbeat(self, gateways: dict[str, dict[str, Any]]) -> None:
        response = self._post(
            "/bridge/agent/heartbeat",
            {"agent_id": self._agent_id, "agent_version": VERSION, "gateways": gateways},
        )
        if response.status_code != 200:
            raise PlatformError(
                f"Heartbeat failed: HTTP {response.status_code} {response.text[:300]}"
            )
