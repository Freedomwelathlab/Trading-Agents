"""`python -m bridge_agent` - start the bridge agent.

`python -m bridge_agent --check` runs one heartbeat (gateway status +
platform auth) and exits: 0 when every configured gateway is reachable and
authenticated, 1 otherwise. Use it before scheduling the agent.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler

from bridge_agent import VERSION
from bridge_agent.agent import BridgeAgent
from bridge_agent.config import AgentConfig, ConfigError, load_config
from bridge_agent.gateway import Gateway
from bridge_agent.platform_client import PlatformClient, PlatformError


def build_gateways(config: AgentConfig) -> dict[str, Gateway]:
    gateways: dict[str, Gateway] = {}
    if "ibkr" in config.providers:
        from bridge_agent.ibkr import IbkrGateway

        gateways["ibkr"] = IbkrGateway(
            base_url=config.ibkr_gateway_url,
            account_id=config.ibkr_account_id,
            verify_tls=config.ibkr_verify_tls,
        )
    if "moomoo" in config.providers:
        from bridge_agent.moomoo import MoomooGateway

        gateways["moomoo"] = MoomooGateway(
            host=config.moomoo_host,
            port=config.moomoo_port,
            account_id=config.moomoo_account_id,
            trade_password=config.moomoo_trade_password,
            security_firm=config.moomoo_security_firm,
        )
    return gateways


def _configure_logging(config: AgentConfig) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if config.log_file:
        handlers.append(
            RotatingFileHandler(
                config.log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
        )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    # httpx logs every request URL at INFO; keep the log about jobs.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bridge_agent", description=__doc__)
    parser.add_argument("--check", action="store_true", help="one heartbeat, then exit")
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"bridge_agent: configuration error: {exc}", file=sys.stderr)
        return 2
    _configure_logging(config)
    log = logging.getLogger("bridge_agent")
    log.info(
        "bridge agent %s starting: agent_id=%s providers=%s platform=%s",
        VERSION,
        config.agent_id,
        ",".join(config.providers),
        config.platform_url,
    )

    platform = PlatformClient(
        base_url=config.platform_url,
        token=config.token,
        agent_id=config.agent_id,
        claim_wait_seconds=config.claim_wait_seconds,
    )
    agent = BridgeAgent(
        platform=platform,
        gateways=build_gateways(config),
        heartbeat_seconds=config.heartbeat_seconds,
        tickle_seconds=config.tickle_seconds,
        order_min_seconds_left=config.order_min_seconds_left,
    )

    if args.check:
        try:
            reports = agent.heartbeat_once()
        except PlatformError as exc:
            log.error("platform check failed: %s", exc)
            return 1
        for name, report in reports.items():
            log.info("%s: %s", name, report)
        return 0 if all(r["reachable"] and r["authenticated"] for r in reports.values()) else 1

    signal.signal(signal.SIGINT, lambda *_: agent.stop())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: agent.stop())
    agent.run_forever()
    platform.close()
    log.info("bridge agent stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
