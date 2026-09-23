"""Liveness and readiness probes (Phase 41, docs/DECISIONS.md D054).

Two endpoints, two different questions, deliberately not merged into one:

`GET /health` — **liveness**. "Is this process alive and serving HTTP?"
Answers from in-process configuration only; touches no database, no cache,
no vendor. Its response body is byte-for-byte the contract documented in
docs/API.md since Phase 1 and is not changed here, because things already
depend on it (`apps/web/app/api/health/route.ts`, `tests/test_health.py`).

`GET /health/ready` — **readiness**. "Can this process actually serve a
request that needs the database?" Issues a real `SELECT 1` through the
app's own engine and returns 503 when it cannot.

Why the split matters rather than just making `/health` honest:

A liveness probe that fails when a *dependency* is down is a known
production footgun. An orchestrator restarts a container whose liveness
probe fails; restarting an application process does not fix an unreachable
Postgres, so a dependency-checking liveness probe converts a database
outage into an unbounded crash-loop across every replica, which then also
destroys the in-process state (and the log continuity) you need to debug
the outage. Readiness is the probe that is *supposed* to fail on a
dependency outage: it pulls the instance out of the load-balancer rotation
and puts it back, with no restart, the moment the database returns.

The inverse failure is the one the review flagged: before this phase the
only probe was `/health`, and it reported `"status": "ok"` unconditionally.
An instance whose database was unreachable advertised itself as healthy,
so an orchestrator would have kept routing traffic to it. That is strictly
worse than having no probe, because it is a *confident wrong answer*.

Semantics are Kubernetes-shaped but nothing here is Kubernetes-specific —
`/health` and `/health/ready` are plain HTTP and work equally well for a
compose healthcheck, an ALB target group, or a uptime monitor.

**No Redis check, on purpose.** Redis is provisioned in
`docker-compose.yml` and `Settings.redis_url` exists, but as of this phase
no Python code in this repository opens a Redis connection — the `redis`
package is an unused declared dependency (the same fact recorded in
`apps/api/app/portfolio/cycle_lock.py` and D047, re-confirmed by grep for
this phase). A readiness check for a connection the app never makes would
be fabricated signal: it could fail the whole service over a dependency no
request path needs, and it would report on a client whose configuration
has never been exercised. When the first real Redis client lands, its
check belongs here — and not before.
"""

import asyncio
from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from apps.api.app.core.config import Settings, get_settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_engine

router = APIRouter(tags=["health"])

logger = get_logger(__name__)


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness. Cheap, dependency-free, and always 200 while the process
    is able to answer at all — see the module docstring for why this one
    must NOT check the database.

    Phase 49 (D066) added `live_order_reconciler`. That is an ADDITIVE key,
    and it is the same kind of value the two beside it already are: a plain
    read of in-process `Settings`, no I/O, no dependency. The "body shape
    unchanged since Phase 1" note above was about not *breaking* the
    contract — every existing consumer reads named keys and is unaffected —
    and it must stay true of the property that actually matters here, which
    is that this handler touches nothing. It still does not.

    Why it is worth a key at all: the reconciler is the only background job
    in this system that reaches a real trading venue, and whether it is
    running is otherwise visible only in a startup log line that has long
    since scrolled away. An operator asking "is anything going to resolve my
    unconfirmed orders?" should be able to get a straight answer from an
    unauthenticated probe without reading logs or the process's environment.
    """
    settings = get_settings()
    return {
        "status": "ok",
        "trading_mode": settings.trading_mode.value,
        "live_trading_enabled": settings.live_trading_enabled,
        # "enabled:<n>s" states only that the LOOP runs on that interval.
        # It is deliberately NOT a claim that anything can be reconciled:
        # with no live path configured every cycle short-circuits, which is
        # what `live_trading_enabled: false` above already tells the reader.
        # Same NOT_CONFIGURED/DISABLED vocabulary as the startup log.
        "live_order_reconciler": (
            f"enabled:{settings.live_order_reconciler_interval_seconds}s"
            if settings.live_order_reconciler_enabled
            else "DISABLED"
        ),
        # Phase 83: whether the equity market-data credential trio is
        # present. A boolean read of Settings — no value, no I/O — so an
        # operator can tell "vendor not wired" from "vendor wired, market
        # closed" without a session. The same all-or-nothing rule the
        # provider builders apply (D015).
        "market_data": (
            "configured"
            if settings.longport_app_key
            and settings.longport_app_secret
            and settings.longport_access_token
            else "NOT_CONFIGURED"
        ),
        "autotrade_runner": (
            f"enabled:{settings.autotrade_runner_interval_seconds}s"
            if settings.autotrade_runner_enabled
            else "DISABLED"
        ),
        # Phase 90: whether the agent layer has a provider at all. Same
        # reasoning as `market_data` above and the same all-or-nothing gate
        # `build_llm_provider` applies (D018, D104) — a key without a model
        # is not a configured provider.
        #
        # It reports PRESENCE, never validity. A wrong key is
        # indistinguishable from a right one without spending a request at
        # the vendor, and a liveness probe that bills someone is not a
        # liveness probe. The provider's own errors now name the status
        # (D104), which is where an invalid key surfaces.
        #
        # No value, no prefix, no length: this says configured or not, and
        # WHICH VARIABLE IS ABSENT when it is not. An unauthenticated
        # endpoint must never leak the shape of a secret, but the NAME of
        # an environment variable the operator themselves chose to set is
        # not a secret — and naming it is the difference between "check
        # your config" and a fix. Measured need: a deployment reported
        # NOT_CONFIGURED after the operator had set credentials, and
        # nothing on this endpoint could say which half was missing.
        "llm_provider": _llm_provider_status(settings),
    }


def _llm_provider_status(settings: Settings) -> str:
    """`configured`, or `NOT_CONFIGURED: missing X[, Y]`.

    The same all-or-nothing gate `build_llm_provider` applies (D018,
    D104): a key without a model is not a configured provider, and
    reporting it as one would make the agent's 400 look like a bug rather
    than a missing variable.

    Presence only — never validity. A wrong key is indistinguishable from
    a right one without spending a request at the vendor, and a liveness
    probe that bills someone is not a liveness probe. An invalid key
    surfaces through the provider's own error, which names the HTTP status
    (D104).
    """
    missing = [
        name
        for name, value in (
            ("LLM_PROVIDER_API_KEY", settings.llm_provider_api_key),
            ("LLM_PROVIDER_MODEL", settings.llm_provider_model),
        )
        if not value
    ]
    return "configured" if not missing else f"NOT_CONFIGURED: missing {', '.join(missing)}"


async def _check_database(timeout_seconds: float) -> dict[str, Any]:
    """Run `SELECT 1` on the app's own engine under a hard timeout.

    Returns a check dict rather than raising, so the caller can report
    every dependency's state in one response even once there is more than
    one dependency to report on.

    The failure branch reports the exception's *type name* and never its
    message. A driver/DSN error message can carry the connection string,
    and the connection string carries the database password — spec §38 and
    docs/TRADING_SAFETY.md forbid that reaching a sink, and an unauthenticated
    endpoint is the last place it may appear. The type name is enough to
    tell an operator apart a refused connection from an auth failure from
    a timeout, which is what a probe owes them.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            engine = get_engine()
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except TimeoutError:
        return {
            "status": "error",
            "reason": "timeout",
            "timeout_seconds": timeout_seconds,
        }
    except Exception as exc:  # noqa: BLE001 - a probe must classify, never propagate
        return {
            "status": "error",
            "reason": "connection_failed",
            "error_type": type(exc).__name__,
        }
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(response: Response) -> dict[str, Any]:
    """Readiness. 200 only when the database genuinely answered; 503 with
    a named reason otherwise.

    503 is returned via `response.status_code` rather than by raising
    `HTTPException`, so the failing body keeps the same shape as the
    succeeding one (`{"status": ..., "checks": {...}}`) instead of
    collapsing into FastAPI's `{"detail": ...}`. A probe consumer should
    not have to parse two different schemas to find out which dependency
    broke.
    """
    settings = get_settings()
    database = await _check_database(settings.health_readiness_timeout_seconds)
    ready = database["status"] == "ok"

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning(
            "readiness_check_failed",
            check="database",
            reason=database.get("reason"),
            error_type=database.get("error_type"),
        )

    return {
        "status": "ready" if ready else "not_ready",
        "checks": {"database": database},
    }
