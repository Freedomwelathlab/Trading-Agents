from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.app.agents.anthropic_compatible import build_llm_provider
from apps.api.app.agents.fundamental_analyst import build_fundamental_analyst
from apps.api.app.agents.news_analyst import build_news_analyst
from apps.api.app.agents.technical_analyst import build_technical_analyst
from apps.api.app.agents.trader import build_trader_agent
from apps.api.app.api.routes.admin import router as admin_router
from apps.api.app.api.routes.backtests import router as backtests_router
from apps.api.app.api.routes.brokers import router as brokers_router
from apps.api.app.api.routes.emergency_stop import router as emergency_stop_router
from apps.api.app.api.routes.emergency_stop import status_router as emergency_stop_status_router
from apps.api.app.api.routes.health import router as health_router
from apps.api.app.api.routes.marketdata import router as marketdata_router
from apps.api.app.api.routes.orders import fills_router as order_fills_router
from apps.api.app.api.routes.orders import router as orders_router
from apps.api.app.api.routes.portfolio import router as portfolio_router
from apps.api.app.api.routes.strategies import router as strategies_router
from apps.api.app.api.routes.trades import agent_router as agent_trades_router
from apps.api.app.api.routes.trades import router as trades_router
from apps.api.app.api.routes.watchlists import router as watchlists_router
from apps.api.app.auth.routes.login import router as auth_router
from apps.api.app.auth.routes.password_reset import router as password_reset_router
from apps.api.app.auth.routes.session import router as session_router
from apps.api.app.core.config import get_settings
from apps.api.app.core.logging import configure_logging, get_logger
from apps.api.app.core.request_id import RequestIDMiddleware
from apps.api.app.db.base import get_engine, get_session_factory
from apps.api.app.execution.live_broker import build_live_broker_adapter
from apps.api.app.execution.reconciliation import (
    LiveOrderReconciler,
    build_reconciler_cycle_lock,
)
from apps.api.app.marketdata.providers.longbridge import (
    build_longbridge_bar_backfill_provider,
    build_longbridge_fundamentals_provider,
    build_longbridge_history_provider,
    build_longbridge_news_provider,
    build_longbridge_provider,
)
from apps.api.app.marketdata.router import MarketDataRouter
from apps.api.app.notifications.transactional_email import build_email_provider
from apps.api.app.portfolio.cycle_lock import SnapshotCycleLock
from apps.api.app.portfolio.market_hours import MarketHoursGate
from apps.api.app.portfolio.scheduler import PortfolioSnapshotScheduler

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    longbridge = build_longbridge_provider(settings)
    app.state.market_data_router = MarketDataRouter([longbridge]) if longbridge else None
    # D021: same credential gate as the quote provider, but a distinct
    # capability (a price series, not one quote) - see
    # apps/api/app/marketdata/history_provider.py.
    app.state.history_provider = build_longbridge_history_provider(settings)
    # Phase 44 (D059): two more capabilities from the SAME already-
    # credentialed Longbridge relationship - company fundamentals and
    # recent news. Same all-or-nothing credential gate, same
    # NOT_CONFIGURED-means-optional-context posture as the history
    # provider above.
    app.state.fundamentals_provider = build_longbridge_fundamentals_provider(settings)
    app.state.news_provider = build_longbridge_news_provider(settings)
    # Phase 53 (D070): real OHLCV bars over an arbitrary historical date
    # range, for POST /admin/market-data/backfill. Same all-or-nothing
    # Longbridge credential gate as every provider above; None means that
    # endpoint answers NOT_CONFIGURED rather than faking an ingestion.
    app.state.market_data_bar_backfill_provider = build_longbridge_bar_backfill_provider(
        settings
    )

    # Phase 43 (D058): the LIVE broker adapter. Returns None - and
    # therefore constructs no trade context and opens no connection -
    # unless TRADING_MODE=live AND LIVE_TRADING_ENABLED=true AND all three
    # LONGPORT_LIVE_* credentials are set. The repository's committed
    # default has LIVE_TRADING_ENABLED=false, so on a default deployment
    # this is None and POST /brokers/{id}/trades refuses every live-broker
    # request with NOT_CONFIGURED.
    app.state.live_broker_adapter = build_live_broker_adapter(settings)

    # Phase 46 (D063): outbound transactional email, used only by the
    # password-reset flow today. None unless all three EMAIL_PROVIDER_*
    # values are set together - the committed default - in which case the
    # reset flow still issues real tokens and an admin relays the link by
    # hand through POST /admin/users/{id}/password-reset. Nothing ever
    # reports an email as sent when this is None.
    app.state.email_provider = build_email_provider(settings)

    llm_provider = build_llm_provider(settings)
    app.state.trader_agent = build_trader_agent(llm_provider)
    # Phase 16 (D019): same underlying LLM_PROVIDER_* connection as
    # TraderAgent - there's exactly one analyst this phase, so a second,
    # separately-configured provider would be unused parallel infra with
    # nothing to parallelize against (see D019's "future work" note).
    app.state.technical_analyst = build_technical_analyst(llm_provider)
    # Phase 44 (D059): the analyst layer's second and third members. They
    # share the same LLM_PROVIDER_* connection as the trader agent and the
    # technical analyst, for the same reason D019 gave - one configured
    # provider, several single-responsibility agents behind it.
    app.state.fundamental_analyst = build_fundamental_analyst(llm_provider)
    app.state.news_analyst = build_news_analyst(llm_provider)

    # Phase 27 (D030): automatic portfolio snapshots. Opt-in and off by
    # default - when disabled, nothing is constructed and no task runs, so
    # the app behaves exactly as it did through Phase 25/D027's
    # manual-only capture. The scheduler is handed the same
    # MarketDataRouter built above (possibly None / NOT_CONFIGURED); it
    # never sources a price any other way.
    app.state.portfolio_snapshot_scheduler = None
    if settings.portfolio_snapshot_scheduler_enabled:
        scheduler = PortfolioSnapshotScheduler(
            get_session_factory(),
            market_data_router=app.state.market_data_router,
            interval_seconds=settings.portfolio_snapshot_interval_seconds,
            # Phase 35 (D042): weekend gating, on by default. A weekend
            # cycle becomes an in-process no-op instead of a full round of
            # DB queries and vendor quote calls.
            market_hours_gate=MarketHoursGate(
                enabled=settings.portfolio_snapshot_market_hours_gate_enabled
            ),
            # Phase 36 (D044): average unless explicitly configured
            # otherwise. Whichever method is used is recorded on every row,
            # so GET .../history never mixes incomparable P&L silently.
            cost_basis_method=settings.portfolio_snapshot_cost_basis_method,
            # Phase 38 (D047): cross-worker mutual exclusion, on by
            # default. This lifespan runs once per uvicorn/gunicorn
            # worker, so without it every worker would append its own row
            # per interval to an append-only series. With one worker the
            # lock is always won and the cycle is unchanged.
            cycle_lock=SnapshotCycleLock(
                enabled=settings.portfolio_snapshot_cycle_lock_enabled
            ),
        )
        scheduler.start()
        app.state.portfolio_snapshot_scheduler = scheduler

    # Phase 49 (D066): the LIVE ORDER RECONCILER - a SIBLING of the snapshot
    # scheduler above, not a modification of it. Both are opt-in, both are
    # off by default, both are in-process asyncio tasks owned by this
    # lifespan, and they take different advisory-lock object keys so they
    # never exclude each other.
    #
    # This one is doubly inert. It is not constructed at all unless
    # LIVE_ORDER_RECONCILER_ENABLED is set, and even then every cycle
    # short-circuits with NOT_CONFIGURED unless `live_broker_adapter` above
    # is a real adapter - which needs TRADING_MODE=live AND
    # LIVE_TRADING_ENABLED=true AND the LONGPORT_LIVE_* trio. On the
    # committed defaults it is None and nothing runs; enabled on a default
    # deployment it logs one skip per interval and touches neither the
    # database nor a broker.
    app.state.live_order_reconciler = None
    if settings.live_order_reconciler_enabled:
        reconciler = LiveOrderReconciler(
            get_session_factory(),
            live_broker=app.state.live_broker_adapter,
            interval_seconds=settings.live_order_reconciler_interval_seconds,
            # D047's mechanism on this job's own object key. Matters more
            # here than for snapshots: N workers each resolving the same
            # order means N calls to a real venue and N racing UPDATEs.
            cycle_lock=build_reconciler_cycle_lock(
                enabled=settings.live_order_reconciler_cycle_lock_enabled
            ),
        )
        reconciler.start()
        app.state.live_order_reconciler = reconciler

    logger.info(
        "trading_os_startup",
        trading_mode=settings.trading_mode.value,
        live_trading_enabled=settings.live_trading_enabled,
        # D039: this is the FALLBACK default only - the authoritative
        # emergency-stop state lives in emergency_stop_events and is read
        # per trade submission, so this startup line must not be read as
        # "the stop is/isn't currently on".
        emergency_stop_settings_default=settings.emergency_stop_active,
        # D058: "configured" here means a live TradeContext exists, which
        # requires live mode + the enable flag + the credential trio. It
        # does NOT mean an order will be placed - every live submission
        # still needs trade:submit:live, an explicit per-request
        # `confirm: true`, and every gate a paper trade goes through.
        live_broker=("longbridge" if app.state.live_broker_adapter else "NOT_CONFIGURED"),
        market_data_vendor="longbridge" if longbridge else "NOT_CONFIGURED",
        history_provider="longbridge" if app.state.history_provider else "NOT_CONFIGURED",
        fundamentals_provider=(
            "longbridge" if app.state.fundamentals_provider else "NOT_CONFIGURED"
        ),
        news_provider="longbridge" if app.state.news_provider else "NOT_CONFIGURED",
        market_data_bar_backfill_provider=(
            "longbridge" if app.state.market_data_bar_backfill_provider else "NOT_CONFIGURED"
        ),
        # D063: NOT_CONFIGURED here does NOT disable password resets - it
        # means the link is delivered by an admin rather than by email.
        email_provider="configured" if app.state.email_provider else "NOT_CONFIGURED",
        llm_provider="configured" if llm_provider else "NOT_CONFIGURED",
        technical_analyst="configured" if app.state.technical_analyst else "NOT_CONFIGURED",
        fundamental_analyst=(
            "configured" if app.state.fundamental_analyst else "NOT_CONFIGURED"
        ),
        news_analyst="configured" if app.state.news_analyst else "NOT_CONFIGURED",
        portfolio_snapshot_scheduler=(
            f"enabled:{settings.portfolio_snapshot_interval_seconds}s"
            if settings.portfolio_snapshot_scheduler_enabled
            else "DISABLED"
        ),
        portfolio_snapshot_market_hours_gate=(
            "weekend_utc"
            if settings.portfolio_snapshot_market_hours_gate_enabled
            else "DISABLED"
        ),
        portfolio_snapshot_cost_basis_method=(
            settings.portfolio_snapshot_cost_basis_method.value
        ),
        portfolio_snapshot_cycle_lock=(
            "pg_advisory" if settings.portfolio_snapshot_cycle_lock_enabled else "DISABLED"
        ),
        # D066: "enabled" here means the LOOP is running, not that anything
        # will be reconciled. With live_broker=NOT_CONFIGURED above, every
        # cycle short-circuits before touching the database or a broker.
        live_order_reconciler=(
            f"enabled:{settings.live_order_reconciler_interval_seconds}s"
            if settings.live_order_reconciler_enabled
            else "DISABLED"
        ),
        live_order_reconciler_cycle_lock=(
            "pg_advisory" if settings.live_order_reconciler_cycle_lock_enabled else "DISABLED"
        ),
    )
    yield

    # Phase 42 (D056): explicit, ORDERED shutdown. Application-level
    # background work is stopped FIRST, then the database engine it depends
    # on is disposed. The order is not cosmetic: `stop()` cancels the
    # scheduler task and then `await`s it to completion (see
    # PortfolioSnapshotScheduler.stop), so by the time it returns no cycle
    # can still be holding a session from the pool. Disposing first would
    # race a mid-flight cycle against a closing pool.
    if app.state.portfolio_snapshot_scheduler is not None:
        await app.state.portfolio_snapshot_scheduler.stop()

    # D066: the reconciler is stopped in the same phase and for the same
    # reason - `stop()` cancels its task and then awaits it, so by the time
    # this returns no cycle can still hold a pooled session or be mid-way
    # through committing a resolved order. Stopped alongside the scheduler,
    # before the engine below is disposed; the relative order of these two
    # does not matter because they share nothing but the pool.
    if app.state.live_order_reconciler is not None:
        await app.state.live_order_reconciler.stop()

    # The engine created at import time in apps/api/app/db/base.py owns a
    # live asyncpg connection pool. Process exit reclaims those sockets
    # anyway, but that is the OS cleaning up after us, not the service
    # shutting down. Under an orchestrator issuing SIGTERM with a grace
    # period, disposing explicitly returns connections to Postgres
    # deterministically instead of leaving them for the server's own
    # timeout to reap. `get_engine()` (Phase 41/D054) is reused rather than
    # constructing a second engine, so this disposes the exact pool every
    # request and the readiness probe draw from.
    await get_engine().dispose()
    logger.info("trading_os_shutdown_complete")


app = FastAPI(title="Trading OS API", version="0.1.0", lifespan=lifespan)
# Phase 42 (D056): outermost middleware, so the correlation ID is bound
# before any routing, auth, or exception handling runs and is therefore
# present on every log line those layers emit too - including the lines
# describing a request that never reaches a route handler at all.
app.add_middleware(RequestIDMiddleware)
app.include_router(auth_router)
# Phase 46 (D063): the only unauthenticated routes besides /auth/login and
# the health probes. Registered next to the login router they belong with.
app.include_router(password_reset_router)
app.include_router(session_router)
app.include_router(trades_router)
app.include_router(agent_trades_router)
app.include_router(admin_router)
# D039: registered before nothing in particular but kept next to the admin
# router it shares a prefix with. The status route is a separate router
# because it is authentication-only, not admin:manage-gated.
app.include_router(emergency_stop_router)
app.include_router(emergency_stop_status_router)
app.include_router(marketdata_router)
app.include_router(portfolio_router)
# Phase 48 (D065): read-only order/fill history. Registered next to the
# portfolio router rather than the trades router because it is the same
# kind of thing - a broker-scoped read gated on `portfolio:view` - and
# routes/trades.py deliberately stays the file that holds only the
# entrypoints which can move a trade toward a broker.
app.include_router(orders_router)
app.include_router(order_fills_router)
app.include_router(backtests_router)
app.include_router(brokers_router)
# Phase 50: user-scoped, not broker-scoped - registered next to the
# market-data router it shares a resolution path with rather than with the
# /brokers routes it shares no scoping rule with.
app.include_router(watchlists_router)
# Phase 54: user-scoped like the watchlists router above (owned by one
# user, never gated on a BrokerGrant), so it is registered next to it
# rather than with the broker-scoped routers - the difference from
# watchlists is that it additionally requires the strategy:manage
# permission at the router level.
app.include_router(strategies_router)
# Phase 41 (D054): liveness (`/health`, unchanged contract) and the new
# readiness probe (`/health/ready`) moved out of this module into their own
# router - see apps/api/app/api/routes/health.py for why the two are
# separate endpoints rather than one endpoint that checks the database.
app.include_router(health_router)
