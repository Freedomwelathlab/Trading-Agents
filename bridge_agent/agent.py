"""The agent loop: heartbeat, keep the gateway session alive, claim, run,
report.

Two threads. The heartbeat thread reports gateway state every
`BRIDGE_HEARTBEAT_SECONDS` and tickles IBKR every `IBKR_TICKLE_SECONDS`;
the main thread long-polls for jobs and runs them one at a time. One at a
time on purpose: orders to one account are serialised, as they would be
from a person at a desk.

**A job past its deadline is refused, not run.** The platform tells the
agent how many seconds are left; an order with less than
`BRIDGE_ORDER_MIN_SECONDS_LEFT` remaining is refused before it touches the
gateway, because an order placed after the platform stopped waiting is an
order nobody on the platform side knows about.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from bridge_agent.gateway import Gateway, GatewayError
from bridge_agent.platform_client import PlatformClient, PlatformError

log = logging.getLogger("bridge_agent")

MONEY_KINDS = frozenset({"order_submit", "cancel"})


class BridgeAgent:
    def __init__(
        self,
        *,
        platform: PlatformClient,
        gateways: dict[str, Gateway],
        heartbeat_seconds: float = 15.0,
        tickle_seconds: float = 60.0,
        order_min_seconds_left: float = 8.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._platform = platform
        self._gateways = gateways
        self._heartbeat_seconds = heartbeat_seconds
        self._tickle_seconds = tickle_seconds
        self._order_min_seconds_left = order_min_seconds_left
        self._clock = clock
        self._stop = threading.Event()
        self._last_tickle = 0.0

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._gateways))

    def stop(self) -> None:
        self._stop.set()

    # --- heartbeat ------------------------------------------------------------

    def heartbeat_once(self) -> dict[str, dict[str, Any]]:
        reports: dict[str, dict[str, Any]] = {}
        for name, gateway in self._gateways.items():
            try:
                reports[name] = gateway.status().as_report()
            except Exception as exc:  # noqa: BLE001 - a status check must never kill the loop
                reports[name] = {
                    "reachable": False,
                    "authenticated": False,
                    "detail": f"status check failed: {type(exc).__name__}: {exc}"[:500],
                }
        now = self._clock()
        if now - self._last_tickle >= self._tickle_seconds:
            self._last_tickle = now
            for name, gateway in self._gateways.items():
                if reports.get(name, {}).get("authenticated"):
                    try:
                        gateway.keepalive()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("%s keepalive failed: %s", name, exc)
        self._platform.heartbeat(reports)
        return reports

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                reports = self.heartbeat_once()
                log.debug("heartbeat sent: %s", reports)
            except PlatformError as exc:
                log.warning("heartbeat failed: %s", exc)
            except Exception:  # noqa: BLE001
                log.exception("heartbeat crashed; continuing")
            self._stop.wait(self._heartbeat_seconds)

    # --- jobs -----------------------------------------------------------------

    def execute(
        self, job: dict[str, Any], *, received_at: float
    ) -> tuple[bool, dict[str, Any] | None, str | None]:
        """Run one claimed job. Returns (ok, result, error)."""
        kind = str(job.get("kind"))
        provider = str(job.get("provider"))
        gateway = self._gateways.get(provider)
        if gateway is None:
            return False, None, f"This agent does not serve {provider}."
        remaining = float(job.get("expires_in_seconds") or 0) - (self._clock() - received_at)
        minimum = self._order_min_seconds_left if kind in MONEY_KINDS else 1.0
        if remaining < minimum:
            return (
                False,
                None,
                f"EXPIRED_BEFORE_EXECUTION: only {remaining:.1f} s of the platform's wait were "
                f"left (need {minimum:.0f} s for {kind}); nothing was sent to {provider}.",
            )
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        try:
            result = gateway.handle(kind, dict(payload or {}), job_id=str(job["id"]))
        except GatewayError as exc:
            return False, None, str(exc)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed silently
            log.exception("job %s (%s %s) crashed", job.get("id"), provider, kind)
            return False, None, f"agent error: {type(exc).__name__}: {exc}"
        return True, result, None

    def process_one(self) -> bool:
        """Claim (long-poll) and run at most one job. True if one ran."""
        job = self._platform.claim(self.providers)
        if job is None:
            return False
        received_at = self._clock()
        log.info("job %s: %s %s", job.get("id"), job.get("provider"), job.get("kind"))
        ok, result, error = self.execute(job, received_at=received_at)
        outcome = self._platform.post_result(str(job["id"]), ok=ok, result=result, error=error)
        if ok:
            log.info("job %s: done (%s)", job.get("id"), outcome)
        else:
            log.warning("job %s: failed (%s): %s", job.get("id"), outcome, error)
        return True

    def run_forever(self) -> None:
        beat = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        beat.start()
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self.process_one()
                backoff = 1.0
            except PlatformError as exc:
                log.warning("platform: %s (retrying in %.0f s)", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
            except Exception:  # noqa: BLE001
                log.exception("claim loop crashed; retrying")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
        for gateway in self._gateways.values():
            gateway.close()
