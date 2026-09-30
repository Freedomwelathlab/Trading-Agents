"""What every local gateway handler looks like to the agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol


class GatewayError(Exception):
    """The gateway (or this agent, on its behalf) refused a job. The message
    is sent to the platform verbatim as the job's error - so it must never
    contain a secret."""


class GatewayUnreachableError(GatewayError):
    """Nothing answered at the gateway's address at all."""


@dataclass(frozen=True)
class GatewayStatus:
    reachable: bool
    authenticated: bool
    detail: str = ""

    def as_report(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "authenticated": self.authenticated,
            "detail": self.detail[:500],
        }


class Gateway(Protocol):
    name: str

    def status(self) -> GatewayStatus: ...

    def keepalive(self) -> None: ...

    def handle(self, kind: str, payload: dict[str, Any], *, job_id: str) -> dict[str, Any]: ...

    def close(self) -> None: ...


def decimal_text(raw: Any, *, what: str) -> str:
    """A venue number as an exact decimal STRING for the platform. A value
    that is missing or unreadable is an error, never a zero."""
    if raw is None or raw == "":
        raise GatewayError(f"The gateway returned no {what}.")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise GatewayError(f"The gateway returned an unreadable {what}: {raw!r}") from exc
    if not value.is_finite():
        raise GatewayError(f"The gateway returned a non-finite {what}: {raw!r}")
    return format(value, "f")


def require(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or str(value).strip() == "":
        raise GatewayError(f"The job is missing `{key}`.")
    return str(value).strip()
