import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apps.api.app.db.base import Base
from apps.api.app.risk.models import Side


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _pg_enum(python_enum: type[enum.Enum], name: str) -> Enum:
    """SQLAlchemy's Enum() sends the Python member NAME by default, not its
    .value - every enum column must pass values_callable or the Postgres
    type mismatches the Python enum's actual string values (e.g. "PAPER"
    sent instead of "paper", which the DB rejects since the migration
    creates the type with lowercase labels)."""
    return Enum(python_enum, name=name, values_callable=lambda e: [member.value for member in e])


class Role(Base):
    """A named permission bundle (e.g. 'analyst', 'risk_admin', 'live_trader').

    Kept separate from User so the authorization layer (apps/api/app/auth/permissions.py)
    can gate the LIVE execution path (spec §56) by role rather than by a
    single boolean on the user - though no live path exists yet to gate.

    `permissions` is a flat list of permission strings (see
    apps.api.app.auth.permissions.Permission) rather than a fixed enum
    column, so a new role can be granted an existing permission by
    inserting/updating a row - no migration required, no code change
    required. Adding a *new* permission still requires a code change (a new
    Permission member and a check that enforces it somewhere).
    """

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    permissions: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    role_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("roles.id"))
    role: Mapped[Role | None] = relationship()

    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    """CONSECUTIVE failed password attempts since the last successful login
    (docs/DECISIONS.md D049). Reset to 0 on every success, so this is a run
    length, not a lifetime total - it is not an audit trail and must not be
    read as one."""

    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When set and in the future, POST /auth/login refuses this account even
    with the correct password (docs/DECISIONS.md D049). NULL means never
    locked; a past value means a lock that has since expired, which the
    login route treats as a clean slate rather than clearing eagerly."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PasswordResetToken(Base):
    """One issued password-reset capability (docs/DECISIONS.md D063,
    migration 0013).

    The raw token exists in exactly two places and neither of them is this
    table: the reset link the user was handed, and the response body of the
    admin endpoint when no email provider is configured. What is stored
    here is `sha256(token)`, for the same reason `users.hashed_password`
    holds a bcrypt digest rather than the password - a database dump must
    not be a set of working reset links. See migration 0013 for why SHA-256
    is the right primitive for a 256-bit random token where bcrypt is the
    right one for a human-chosen password.

    `used_at` means CONSUMED, not specifically "redeemed": redemption sets
    it on the token being redeemed AND on every other still-unused token
    for the same user, so an old link in an older email stops working the
    moment a newer one is used. NULL is the only redeemable state and is
    never restored.

    Rows are never updated other than that one NULL -> timestamp
    transition, and are never deleted by the application. Expired and used
    rows are left in place: they are the only record that a reset was ever
    requested for an account, which is exactly the kind of thing an
    operator wants to be able to look at after the fact.
    """

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        Index("ix_password_reset_tokens_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Watchlist(Base):
    """A named list of symbols one user is researching (Phase 50,
    migration 0015).

    User-scoped, not broker-scoped: a watchlist is a research artifact and
    has nothing to do with which brokers the owner may trade through, so
    there is deliberately no `broker_id` here and no BrokerGrant check on
    any of its routes (see apps/api/app/api/routes/watchlists.py). The
    owner is the only reader and the only writer; there is no sharing
    model and none is implied by this schema.

    Watchlists are created explicitly (`POST /watchlists`), never lazily
    on first read - a GET that silently writes a "default" row would make
    a read endpoint mutate the database, and would leave a phantom empty
    watchlist behind for every user who ever merely looked.

    `user_id` is ON DELETE CASCADE for the same reason
    `password_reset_tokens.user_id` is (D063): a watchlist has no meaning
    independent of the person whose research it describes, unlike
    `orders.submitted_by_user_id`, which is deliberately nullable so an
    order's audit trail outlives its submitter. Nothing here is an audit
    trail; these rows are working state, and they are the one kind of row
    in this schema a user may freely delete.
    """

    __tablename__ = "watchlists"
    __table_args__ = (Index("ix_watchlists_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    items: Mapped[list["WatchlistItem"]] = relationship(
        order_by="WatchlistItem.symbol", cascade="all, delete-orphan"
    )


class WatchlistItem(Base):
    """One symbol on one watchlist (Phase 50, migration 0015).

    `(watchlist_id, symbol)` is UNIQUE, so a symbol cannot appear twice on
    the same list - the second add is a 409, not a duplicate row that
    would then produce the same quote twice in
    `GET /watchlists/{id}/quotes`. Symbols are normalized (trimmed and
    upper-cased) by the route before they reach this table, so the
    constraint is a real one rather than one "aapl" can walk around.

    `symbol` is a plain String, denormalized rather than FK'd to `assets`,
    for the same reason `orders.symbol` is (D006): no asset-lookup service
    exists, and a watchlist must be able to hold a symbol the user is
    researching precisely because the system has never traded it.
    """

    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint("watchlist_id", "symbol", name="uq_watchlist_item_watchlist_symbol"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    watchlist_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("watchlists.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AssetClass(str, enum.Enum):  # noqa: UP042 (str mixin kept for SQLAlchemy Enum interop)
    EQUITY = "equity"
    ETF = "etf"
    OPTION = "option"
    FUTURE = "future"
    CRYPTO = "crypto"
    FOREX = "forex"


class Asset(Base):
    """A tradeable instrument. Deliberately vendor-agnostic — symbology per
    broker/data-vendor lives on BrokerAssetMapping, not here."""

    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("symbol", "asset_class", name="uq_asset_symbol_class"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    asset_class: Mapped[AssetClass] = mapped_column(
        _pg_enum(AssetClass, "assetclass"), nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BrokerKind(str, enum.Enum):  # noqa: UP042 (str mixin kept for SQLAlchemy Enum interop)
    PAPER = "paper"
    LIVE = "live"


class Broker(Base):
    """A configured broker/exchange connection.

    `kind` is the structural mechanism that keeps paper and live credentials
    from ever occupying the same row (spec §51). The as-yet-unbuilt
    execution layer must refuse to construct a LiveContext from a row where
    kind != 'live', and vice versa.
    """

    __tablename__ = "brokers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[BrokerKind] = mapped_column(_pg_enum(BrokerKind, "brokerkind"), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BrokerAccount(Base):
    """Current cash balance for one (paper) broker. One row per broker,
    created lazily the first time apps.api.app.execution.persistence loads
    a broker that has no row yet (seeded with Settings.paper_broker_starting_cash).

    This is mutable, current-state data - unlike Order/Fill, it is NOT
    append-only. It is the thing Order/Fill's append-only history exists
    to make reconstructible if this row is ever lost or wrong; the two are
    complementary, not duplicates of the same information."""

    __tablename__ = "broker_accounts"

    broker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brokers.id"), primary_key=True)
    cash: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BrokerPosition(Base):
    """Current open-position quantity for one (broker, symbol) pair.
    Mutable current-state, like BrokerAccount - not append-only. Rows are
    only kept for nonzero quantities; a position closed back to zero is
    deleted rather than kept as a zero row."""

    __tablename__ = "broker_positions"
    __table_args__ = (
        UniqueConstraint("broker_id", "symbol", name="uq_broker_position_broker_symbol"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    broker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brokers.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BrokerGrant(Base):
    """Explicit per-user, per-broker access grant. A user must hold both
    the relevant Role permission (apps.api.app.auth.permissions) AND a
    BrokerGrant row for a specific broker to act on it - the permission
    says what kind of thing they may do in general, the grant says which
    specific broker they may do it to. Neither alone is sufficient.

    No self-service grant endpoint exists yet - rows are created directly,
    same pattern as User/Role (see docs/DECISIONS.md D010/D011)."""

    __tablename__ = "broker_grants"
    __table_args__ = (UniqueConstraint("user_id", "broker_id", name="uq_broker_grant_user_broker"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    broker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brokers.id"), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderStatus(str, enum.Enum):  # noqa: UP042 (str mixin kept for SQLAlchemy Enum interop)
    FILLED = "filled"
    REJECTED = "rejected"


class Order(Base):
    """Append-only record of every trade proposal the OMS decided on -
    approved or rejected. Never updated after insert; a re-attempt is a new
    row, not a status change on this one, so the audit trail always shows
    exactly what was actually decided at the time (spec Sec17).

    Money columns are NUMERIC, never float (spec requirement for anything
    financial). `symbol` is denormalized rather than FK'd to `assets` - no
    asset-lookup/creation service exists yet; revisit once one does.
    """

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_broker_symbol_submitted", "broker_id", "symbol", "submitted_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    broker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brokers.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[Side] = mapped_column(_pg_enum(Side, "side"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    estimated_price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    status: Mapped[OrderStatus] = mapped_column(
        _pg_enum(OrderStatus, "orderstatus"), nullable=False
    )
    risk_block_reason: Mapped[str | None] = mapped_column(String(64))
    """The RiskDecision.reason.value that caused a rejection, if any. Null
    when status is FILLED."""
    risk_detail: Mapped[str | None] = mapped_column(String(500))
    portfolio_action: Mapped[str | None] = mapped_column(String(32))
    """The PortfolioAction.value the trade-path Portfolio Manager returned
    (D029, migration 0009). Null means no Portfolio Manager ran for this
    order - either the Risk Engine rejected it first, or the caller supplied
    no portfolio state. Never read null as 'the portfolio approved it'."""
    portfolio_binding_constraint: Mapped[str | None] = mapped_column(String(64))
    """The PortfolioConstraint.value that forced a MODIFY or a REJECT. Null
    on APPROVE, and null when no Portfolio Manager ran."""
    portfolio_detail: Mapped[str | None] = mapped_column(String(500))
    portfolio_requested_quantity: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    """The quantity the risk-approved proposal carried BEFORE the Portfolio
    Manager saw it. `quantity` above is always the quantity actually acted
    on, so these two differ exactly when portfolio_action is 'modify' -
    which is what makes a resize visible in the audit trail rather than
    silently rewriting history (spec Sec17/Sec18)."""
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    """Nullable because not every future order-creation path may have an
    authenticated human behind it (e.g. an automated agent) - but the
    current HTTP endpoint always sets it (spec Sec17 wants the decision
    chain to say who, not just what)."""
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Fill(Base):
    """One row per fill. A 1:1 relationship with Order today because the
    paper broker only ever produces a single full fill - this table exists
    separately from Order (rather than columns on it) so partial fills are
    a schema-compatible addition later, not a migration that reshapes
    Order."""

    __tablename__ = "fills"

    id: Mapped[uuid.UUID] = _uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id"), nullable=False, unique=True
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EmergencyStopEvent(Base):
    """Append-only history of every emergency-stop flip (docs/DECISIONS.md
    D039). The CURRENT state of the kill switch is the `active` value of
    the highest-`id` row; there is no row that gets updated in place, so
    the state and its audit trail are the same object and cannot drift
    apart.

    Empty table means "never flipped", in which case the caller falls back
    to `Settings.emergency_stop_active` as the documented default (D039) -
    it is a bootstrap default, not an override: once any row exists,
    `Settings` is no longer consulted.

    `id` is a monotonic BigInteger identity rather than this schema's usual
    random `uuid4` PK, deliberately: "latest row wins" has to be a total,
    unambiguous order, and two flips within the same clock tick would make
    `created_at` alone ambiguous. This is the one table whose ordering is
    load-bearing for a safety control, so it gets a sequence.

    `reason` is NOT NULL on activation by API contract (the request schema
    requires it) and is also required on deactivation - a kill switch that
    can be turned off with no recorded justification is not an audited
    control. `actor_user_id` is the authenticated caller who flipped it,
    FK'd to users like orders.submitted_by_user_id.
    """

    __tablename__ = "emergency_stop_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PortfolioSnapshotRow(Base):
    """Append-only persisted history of a `compute_portfolio_snapshot()`
    call (docs/DECISIONS.md D027) - like Order/Fill, never updated after
    insert. Each row is only ever written from that function's real output
    (same DATA_UNAVAILABLE-on-missing-mark discipline as the live
    endpoint); nothing here is fabricated, interpolated, or written by a
    scheduler (D027 explicitly scopes this to caller-triggered snapshots
    only).

    Named with a `Row` suffix (matching `OrderRow`/`FillRow`'s aliasing in
    apps/api/app/portfolio/snapshot.py) to keep it distinct from the
    Pydantic `PortfolioSnapshot` in apps/api/app/portfolio/models.py -
    that class is the computed, in-memory value; this class is its
    persisted-row counterpart, a different concern.
    """

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        Index("ix_portfolio_snapshots_broker_captured", "broker_id", "captured_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    broker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brokers.id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    cash: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    total_equity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    total_unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    total_realized_pnl: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    cost_basis_method: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="average"
    )
    """Which `CostBasisMethod` (apps/api/app/portfolio/models.py) produced
    this row's `avg_cost`/`unrealized_pnl`/`realized_pnl` figures
    (docs/DECISIONS.md D044). Stored as the enum's plain string *value*,
    not a Postgres ENUM type, matching how every other enum in this schema
    is persisted (`orders.side`, `orders.status`, ...) - adding a method
    later is then an application change, not a migration that mutates a
    type every existing row depends on.

    NOT NULL with `server_default='average'` so every row written before
    D044's migration reads back as `average` - which is exactly what those
    rows are, since the write path was average-only until then (D041 scope
    decision (c)). A nullable column would have made the historical rows
    read as "unknown method", which is strictly less true than the fact we
    actually have.
    """

    positions: Mapped[list["PortfolioSnapshotPositionRow"]] = relationship(
        order_by="PortfolioSnapshotPositionRow.symbol"
    )


class PortfolioSnapshotPositionRow(Base):
    """One row per open position captured in a given `PortfolioSnapshotRow`
    (docs/DECISIONS.md D027). A child table, not a JSON column on the
    parent - see D027 for the full reasoning; in short, this makes
    "show me AAPL's position history across every snapshot" a plain
    indexed query (`ix_portfolio_snapshot_positions_symbol`) instead of a
    JSON-path scan, matching every other per-symbol time series in this
    schema (Order/Fill)."""

    __tablename__ = "portfolio_snapshot_positions"
    __table_args__ = (
        Index("ix_portfolio_snapshot_positions_snapshot", "snapshot_id"),
        Index("ix_portfolio_snapshot_positions_symbol", "symbol"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_snapshots.id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    current_value: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
