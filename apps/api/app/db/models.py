import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
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
