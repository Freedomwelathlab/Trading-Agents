"""Agent configuration from the environment and an optional `.env` file.

No python-dotenv dependency: the format needed here is `KEY=VALUE` lines,
and a real environment variable always wins over the file.
"""

from __future__ import annotations

import os
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

PACKAGE_DIR = Path(__file__).resolve().parent
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
KNOWN_PROVIDERS = ("ibkr", "moomoo")


class ConfigError(ValueError):
    """The agent cannot start with this configuration."""


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def is_local_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in LOCAL_HOSTS


def _sanitize_agent_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "-", value)[:64]
    return cleaned or "bridge-agent"


@dataclass(frozen=True)
class AgentConfig:
    platform_url: str
    token: str = field(repr=False)
    agent_id: str
    providers: tuple[str, ...]
    ibkr_gateway_url: str = "https://localhost:5000/v1/api"
    ibkr_account_id: str | None = None
    moomoo_host: str = "127.0.0.1"
    moomoo_port: int = 11111
    moomoo_account_id: str | None = None
    moomoo_trade_password: str | None = field(default=None, repr=False)
    moomoo_security_firm: str = "FUTUINC"
    heartbeat_seconds: float = 15.0
    tickle_seconds: float = 60.0
    claim_wait_seconds: float = 20.0
    order_min_seconds_left: float = 8.0
    log_file: str | None = "bridge_agent.log"

    @property
    def ibkr_verify_tls(self) -> bool:
        """TLS verification is skipped ONLY for a gateway on this machine,
        whose certificate is IBKR's own self-signed one. Anything else is
        verified - a remote gateway with an unverified certificate is a
        man-in-the-middle away from a stranger reading the account."""
        return not is_local_url(self.ibkr_gateway_url)


def load_config(
    environ: Mapping[str, str] | None = None, *, env_file: Path | None = None
) -> AgentConfig:
    base = dict(os.environ if environ is None else environ)
    # bridge_agent\.env next to this file - NOT the current directory, which
    # may be the platform checkout whose own .env is a different thing.
    file_path = env_file or Path(base.get("BRIDGE_AGENT_ENV_FILE") or PACKAGE_DIR / ".env")
    merged = {**read_env_file(file_path), **base}

    def get(name: str, default: str | None = None) -> str | None:
        value = merged.get(name)
        if value is None or not value.strip():
            return default
        return value.strip()

    platform_url = get("PLATFORM_URL")
    token = get("BRIDGE_AGENT_TOKEN")
    if not platform_url:
        raise ConfigError("PLATFORM_URL is not set (the Trading OS API base URL).")
    if not token:
        raise ConfigError("BRIDGE_AGENT_TOKEN is not set (the same value as on the platform).")
    if len(token) < 32:
        raise ConfigError(
            "BRIDGE_AGENT_TOKEN is shorter than 32 characters; the platform ignores it."
        )
    scheme = urlparse(platform_url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ConfigError(f"PLATFORM_URL must be an http(s) URL, not {platform_url!r}.")
    if scheme == "http" and not is_local_url(platform_url):
        # The bearer token would cross the internet in the clear.
        raise ConfigError("PLATFORM_URL must use https:// unless it points at this machine.")

    providers = tuple(
        p.strip().lower() for p in (get("BRIDGE_PROVIDERS", "ibkr") or "").split(",") if p.strip()
    )
    unknown = [p for p in providers if p not in KNOWN_PROVIDERS]
    if unknown or not providers:
        raise ConfigError(
            f"BRIDGE_PROVIDERS must list one or more of {', '.join(KNOWN_PROVIDERS)}; "
            f"got {get('BRIDGE_PROVIDERS')!r}."
        )

    def number(name: str, default: float) -> float:
        raw = get(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number, not {raw!r}.") from exc
        if value <= 0:
            raise ConfigError(f"{name} must be positive.")
        return value

    return AgentConfig(
        platform_url=platform_url.rstrip("/"),
        token=token,
        agent_id=_sanitize_agent_id(get("BRIDGE_AGENT_ID") or socket.gethostname()),
        providers=providers,
        ibkr_gateway_url=(get("IBKR_GATEWAY_URL") or "https://localhost:5000/v1/api").rstrip("/"),
        ibkr_account_id=get("IBKR_ACCOUNT_ID"),
        moomoo_host=get("MOOMOO_OPEND_HOST", "127.0.0.1") or "127.0.0.1",
        moomoo_port=int(number("MOOMOO_OPEND_PORT", 11111)),
        moomoo_account_id=get("MOOMOO_ACCOUNT_ID"),
        moomoo_trade_password=get("MOOMOO_TRADE_PASSWORD"),
        moomoo_security_firm=(get("MOOMOO_SECURITY_FIRM", "FUTUINC") or "FUTUINC").upper(),
        heartbeat_seconds=number("BRIDGE_HEARTBEAT_SECONDS", 15.0),
        tickle_seconds=number("IBKR_TICKLE_SECONDS", 60.0),
        claim_wait_seconds=min(number("BRIDGE_CLAIM_WAIT_SECONDS", 20.0), 60.0),
        order_min_seconds_left=number("BRIDGE_ORDER_MIN_SECONDS_LEFT", 8.0),
        log_file=_log_path(get("BRIDGE_AGENT_LOG_FILE", "bridge_agent.log")),
    )


def _log_path(value: str | None) -> str | None:
    if not value or value.lower() == "none":
        return None
    path = Path(value)
    return str(path if path.is_absolute() else PACKAGE_DIR / path)
