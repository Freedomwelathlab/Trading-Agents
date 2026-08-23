import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
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

    Kept separate from User so the not-yet-built authorization layer can
    gate the LIVE execution path (spec §56) by role rather than by a single
    boolean on the user.
    """

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
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
