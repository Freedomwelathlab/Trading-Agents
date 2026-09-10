import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
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
    """Rejected by THIS SYSTEM - the Risk Engine, the emergency stop, the
    duplicate-order check or the Portfolio Manager. It does NOT mean a
    broker rejected anything; a venue rejection is BROKER_CLOSED_UNFILLED
    below, with the venue's own word for it in `broker_status`. Keeping the
    two apart matters because a REJECTED order never reached a broker at
    all, while the other did."""

    SUBMITTED_UNCONFIRMED = "submitted_unconfirmed"
    """Phase 49 (docs/DECISIONS.md D066). THE ONLY NON-TERMINAL STATUS. A
    real order exists at a real broker, every gate approved it, and the
    broker has not (yet) reported whether it executed.

    Before this phase such an order left no row at all: the route answered
    `502 LIVE_ORDER_UNCONFIRMED` and the transaction rolled back, so the
    single most consequential thing this system can do - hand a live venue
    a real order - was the one thing its append-only audit trail did not
    record. A row in this status is that record, and it is what
    `LiveOrderReconciler` (apps/api/app/execution/reconciliation.py) later
    resolves into one of the terminal statuses above/below from the
    broker's own answer.

    `broker_order_id` is never null on a row in this status - it is the
    handle the reconciler needs - and `fills` never has a row for one,
    because no fill has been reported."""

    BROKER_CLOSED_UNFILLED = "broker_closed_unfilled"
    """Phase 49 (D066). Terminal. The BROKER reports the order reached an
    end state having executed nothing: cancelled, expired, or rejected by
    the venue. Which of those it was is recorded verbatim in
    `broker_status` rather than being flattened into three more enum values
    - this system acts identically on all of them (stop watching, record
    no fill), and the venue's own wording is more useful to a human reading
    the audit trail than a paraphrase of it would be."""


class Order(Base):
    """Append-only record of every trade proposal the OMS decided on -
    approved or rejected. A re-attempt is a new row, not a status change on
    this one, so the audit trail always shows exactly what was actually
    decided at the time (spec Sec17).

    Money columns are NUMERIC, never float (spec requirement for anything
    financial). `symbol` is denormalized rather than FK'd to `assets` - no
    asset-lookup/creation service exists yet; revisit once one does.

    THE ONE PERMITTED UPDATE (Phase 49, docs/DECISIONS.md D066)
    ----------------------------------------------------------
    Exactly one transition may ever be applied to an existing row:
    `SUBMITTED_UNCONFIRMED` -> `FILLED` or `BROKER_CLOSED_UNFILLED`, by
    `LiveOrderReconciler`, setting `reconciled_at` and `broker_status`. The
    reconciler's UPDATE carries `WHERE status = 'submitted_unconfirmed'`, so
    a row that already reached a terminal status can never be rewritten by
    anything, ever - which is the property the append-only rule actually
    exists to protect.

    That is not a softening of the rule so much as an admission of what it
    was always about. `SUBMITTED_UNCONFIRMED` does not record a DECISION
    this system made; the decision was already complete and unchanging when
    the row was written. It records that a real order was handed to a real
    venue and that the outcome was, at that instant, genuinely unknown.
    Resolving an unknown outcome into the known one the broker later
    reported does not rewrite history - refusing to would leave the audit
    trail permanently, knowably wrong instead.
    """

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_broker_symbol_submitted", "broker_id", "symbol", "submitted_at"),
        # D066: the reconciler's only query - "every unresolved live order" -
        # must not degrade into a full scan of an append-only table that
        # grows forever. Partial, so it indexes only the handful of rows
        # that are ever actually unresolved rather than every order ever
        # placed.
        Index(
            "ix_orders_unconfirmed",
            "broker_id",
            "submitted_at",
            postgresql_where=text("status = 'SUBMITTED_UNCONFIRMED'"),
        ),
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

    broker_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    """Phase 49 (docs/DECISIONS.md D066). The BROKER's own identifier for a
    real order this system placed at a real venue - never a locally
    generated one, and never populated for a paper order (the paper
    simulator has no venue and issues no id). Null for every pre-Phase-49
    row and for every order that never reached a broker at all.

    UNIQUE, because it is the reconciler's join key back to reality and
    because two `orders` rows claiming the same venue order would make the
    audit trail ambiguous about a thing real money moved through. It is
    unique rather than a foreign key for the obvious reason: the row it
    references lives at the broker, not in this database."""

    broker_status: Mapped[str | None] = mapped_column(String(32))
    """Phase 49 (D066). The venue's OWN status string, verbatim and
    unparaphrased - e.g. `Filled`, `Canceled`, `Expired`, `Rejected`. Set by
    the reconciler at the moment it resolves a `SUBMITTED_UNCONFIRMED` row,
    and null before then and on every non-live order.

    This exists so `BROKER_CLOSED_UNFILLED` does not have to fan out into
    three near-identical enum values that this system would treat
    identically anyway. A human reading the audit trail gets the venue's
    actual word for what happened; the enum records only what this system
    concluded from it."""

    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Phase 49 (D066). When `LiveOrderReconciler` resolved this order
    against the broker. Null means never reconciled, which for a
    `SUBMITTED_UNCONFIRMED` row means the outcome is still genuinely
    unknown - deliberately NOT defaulted to `submitted_at`, since 'we have
    not looked yet' and 'we looked and it was still open' must stay
    distinguishable."""


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


class MarketDataBar(Base):
    """One real OHLCV bar for one (symbol, bar_interval) at one timestamp
    (Phase 53, migration 0016, docs/DECISIONS.md D070) - closes the gap
    HistoryProvider (apps/api/app/marketdata/history_provider.py, D021)
    leaves: that Protocol only ever exposes "the most recent N daily
    closes as of now," fetched live from the vendor on every call, with no
    persistence and no arbitrary date-range query. This table is what
    apps/api/app/marketdata/store.py's MarketDataStore reads and writes;
    ingestion is described in apps/api/app/marketdata/ingestion/backfill.py.

    Primary key is the natural composite `(symbol, bar_interval, ts)`, not
    this schema's usual `_uuid_pk()` - the one deliberate departure from
    that helper in the whole schema. A Timescale hypertable's unique
    constraints must include the partitioning column (`ts`), and a
    surrogate UUID PK would add nothing a natural key doesn't already
    give: idempotent re-ingestion via `ON CONFLICT ... DO UPDATE`, and no
    possibility of two rows ever describing the same bar.

    `bar_interval` is a plain string, not a Postgres ENUM, so adding an
    intraday interval later (5m, 1h, ...) is an application change, not a
    migration that mutates a type every existing row depends on - matching
    how `portfolio_snapshots.cost_basis_method` is persisted for the same
    reason. Only '1d' is ever written by Phase 53.

    `open`/`high`/`low`/`volume` are nullable because a vendor may in
    principle return a close-only record; `close` is NOT NULL because a
    bar with no close is not a bar. `source` records real provenance
    (e.g. "longbridge") - never blank, never guessed - so any row can
    always be traced to where it came from.
    """

    __tablename__ = "market_data_bars"
    __table_args__ = (
        Index("ix_market_data_bars_symbol_interval_ts", "symbol", "bar_interval", "ts"),
    )

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    bar_interval: Mapped[str] = mapped_column(String(8), primary_key=True, default="1d")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    high: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    low: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BackfillJobStatus(str, enum.Enum):  # noqa: UP042 (str mixin kept for SQLAlchemy Enum interop)
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    """Reserved for a future multi-symbol/batch backfill that can
    legitimately succeed for some inputs and fail for others within the
    same job row. Phase 53's single-symbol synchronous job never sets
    this - every run terminates SUCCEEDED or FAILED."""


class MarketDataBackfillJob(Base):
    """One record of one manual, on-demand historical-bar backfill attempt
    (Phase 53, migration 0016). Unlike MarketDataBar rows (which an
    ingestion job upserts, possibly many per run), a job row is written
    once and then updated exactly once, in place, from PENDING/RUNNING to
    a terminal status (SUCCEEDED/FAILED) - there is no background worker
    that could race that update, since
    apps/api/app/marketdata/ingestion/backfill.py runs a job to completion
    within the HTTP request that created it.

    `requested_by_user_id` is ON DELETE SET NULL, matching
    `orders.submitted_by_user_id` and `emergency_stop_events.actor_user_id`
    - this is an audit-adjacent operational record whose meaning (what was
    requested, over what range, with what outcome) survives the deletion
    of whoever requested it, unlike a Watchlist's CASCADE.
    """

    __tablename__ = "market_data_backfill_jobs"
    __table_args__ = (
        Index("ix_backfill_jobs_symbol_interval", "symbol", "bar_interval"),
        Index("ix_backfill_jobs_status", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    bar_interval: Mapped[str] = mapped_column(String(8), nullable=False, default="1d")
    requested_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    requested_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[BackfillJobStatus] = mapped_column(
        _pg_enum(BackfillJobStatus, "backfilljobstatus"), nullable=False
    )
    bars_ingested: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    earliest_bar_date: Mapped[date | None] = mapped_column(Date)
    latest_bar_date: Mapped[date | None] = mapped_column(Date)
    error_detail: Mapped[str | None] = mapped_column(String(500))
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class StrategyStatus(str, enum.Enum):  # noqa: UP042 (str mixin kept for SQLAlchemy Enum interop)
    ACTIVE = "active"
    ARCHIVED = "archived"


class StrategyVersionStatus(str, enum.Enum):  # noqa: UP042 (str mixin for SQLAlchemy Enum interop)
    DRAFT = "draft"
    VALIDATED = "validated"
    ARCHIVED = "archived"


class Strategy(Base):
    """One named trading strategy a user is developing (Phase 54, migration
    0017). The strategy row itself carries only identity and lifecycle -
    name, description, active/archived; every actual rule set lives on a
    `StrategyVersion` child, because the thing a backtest is run against
    has to be a specific, frozen definition rather than "whatever this
    strategy says today."

    `owner_user_id` is ON DELETE SET NULL, deliberately NOT the CASCADE
    `Watchlist.user_id` uses. A watchlist is ephemeral personal research
    state with no meaning after its owner is gone; a strategy is not. Phase
    55 adds `backtest_runs.strategy_version_id` as ON DELETE RESTRICT - a
    run's exact input must never disappear out from under the result it
    produced - so a strategy and its versions have to be able to outlive
    the deletion of whoever created them. That is
    `orders.submitted_by_user_id`'s reasoning (an audit trail survives its
    submitter), not `Watchlist.user_id`'s. Note this codebase does not
    delete users at all (D013 documents deactivation as the path), so this
    is a structural guarantee rather than a routine code path.

    There is exactly one owner, set at creation and never reassigned this
    phase - no sharing model, no co-owner join table, and no ADMIN
    override (see Permission.STRATEGY_MANAGE). Every route checks
    `owner_user_id == current_user.id`.

    `status` is the strategy-level lifecycle (`active` / `archived`) and is
    independent of any version's own status: archiving a strategy is how a
    user retires it from their working list without deleting research that
    a backtest run may still reference.
    """

    __tablename__ = "strategies"
    __table_args__ = (Index("ix_strategies_owner_created", "owner_user_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    status: Mapped[StrategyStatus] = mapped_column(
        _pg_enum(StrategyStatus, "strategystatus"),
        nullable=False,
        default=StrategyStatus.ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    """Carries `onupdate` (unlike `Watchlist.created_at`, which has no
    updated_at at all) because `PATCH /strategies/{id}` really does mutate
    this row - name, description and status are all editable - and "when
    was this last touched" is otherwise unanswerable."""

    versions: Mapped[list["StrategyVersion"]] = relationship(
        order_by="StrategyVersion.version_number", cascade="all, delete-orphan"
    )


class StrategyVersion(Base):
    """One frozen-or-being-drafted rule set belonging to one Strategy
    (Phase 54, migration 0017). `(strategy_id, version_number)` is UNIQUE:
    version numbers are dense, start at 1, and are assigned by the
    application as `max(existing) + 1`, so "version 3 of this strategy"
    names exactly one row forever.

    `definition` IS THE FIRST JSONB COLUMN IN THIS SCHEMA. Every other
    model here uses typed columns, and that remains the default - this one
    is the deliberate exception. The rule vocabulary a strategy definition
    expresses is still moving: walk-forward (Phase 57), Monte Carlo, and
    universe scanning (Phase 59) will each want new indicator and operator
    types, and a migration per new rule type - each one reshaping a table
    every existing strategy depends on - would be strictly worse than one
    JSONB column plus application-level structural validation
    (apps/api/app/strategies/validation.py). That validation is what keeps
    the column from being a junk drawer: nothing is ever marked
    `validated` without passing it, and it rejects unknown keys rather
    than ignoring them, so a typo is an error rather than a silently
    dropped rule.

    **A version is immutable once `status != 'draft'`.** That is enforced
    at the API layer as a 409
    (apps/api/app/api/routes/strategies.py - `PATCH .../versions/{id}` and
    the validate route both refuse a non-draft), not by a database
    trigger, so the failure mode a caller sees is an ordinary HTTP error
    naming the fork endpoint to use instead, rather than a raw
    `psycopg`/asyncpg exception surfacing from a trigger nobody can
    catch usefully. This is the concrete mechanism behind "never silently
    mutate a validated strategy": editing a validated version is not
    prevented in the sense of being awkward, it is impossible - the only
    way forward is a new draft forked from it, which leaves the validated
    version exactly as whatever backtest ran against it saw it.

    `definition_hash` is the sha256 of the canonical (sorted-key,
    whitespace-free) JSON of `definition`
    (apps/api/app/strategies/service.py::compute_definition_hash). It is
    stored rather than computed on read so two versions can be compared for
    logical identity - "did this fork actually change anything?" - without
    reparsing two JSON blobs, and so a later phase can key a cached
    backtest result on it.

    `created_by_user_id` is ON DELETE SET NULL for the same reason
    `Strategy.owner_user_id` is: a version is an input to a run whose
    record must outlive its author.
    """

    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version_number", name="uq_strategy_version_number"),
        Index("ix_strategy_versions_strategy_created", "strategy_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[StrategyVersionStatus] = mapped_column(
        _pg_enum(StrategyVersionStatus, "strategyversionstatus"),
        nullable=False,
        default=StrategyVersionStatus.DRAFT,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When this version passed structural validation and became immutable.
    NULL on every draft and never back-filled - "not yet validated" and
    "validated at some unknown time" must stay distinguishable, the same
    reasoning `orders.reconciled_at` documents."""


class BacktestRunStatus(str, enum.Enum):  # noqa: UP042 (str mixin for SQLAlchemy Enum interop)
    """The lifecycle of one persisted backtest (Phase 55, migration 0018).

    Deliberately the same four names `BackfillJobStatus` uses for the same
    reasons, minus its `PARTIAL`: a backtest of one symbol over one window
    either produced a full equity curve or it did not, so there is no
    honest middle state for it to report. PENDING exists for a future
    queued/background run; Phase 55's synchronous engine writes RUNNING and
    then exactly one terminal status inside the request that created it.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class BacktestRun(Base):
    """One persisted execution of one StrategyVersion over one symbol and
    date window (Phase 55, migration 0018) - the durable counterpart to
    D025's in-memory-only `BacktestResult`, which this codebase computed and
    then threw away.

    **`strategy_version_id` is ON DELETE RESTRICT** - not CASCADE, not SET
    NULL, and the distinction is the whole reason `strategy_versions` exists
    as a separate table at all (see `StrategyVersion` and migration 0017,
    which already anticipate this FK). A run's exact, immutable input is the
    version it ran; a result whose input silently vanished is not a weaker
    audit record, it is a misleading one, and a result whose input was
    edited underneath it is worse still. CASCADE would delete real computed
    results as a side effect of tidying up a strategy, and SET NULL would
    leave rows claiming numbers nothing can explain. RESTRICT makes both
    impossible at the schema level.

    Nothing in this phase - or in the several planned after it - deletes a
    `StrategyVersion`; there is no route that can. So this is a structural
    guarantee that stays true rather than a behavior anyone routinely relies
    on, exactly like `Strategy.owner_user_id`'s SET NULL in a codebase that
    does not delete users (D013).

    **Append-only, in `Order`'s sense.** A run row is written once as
    RUNNING and updated exactly once, in place, to a terminal status
    together with its metrics and `completed_at` - the same single permitted
    transition `MarketDataBackfillJob` makes (Phase 53/D070), and for the
    same reason: the engine runs to completion inside the HTTP request that
    created the row, so no background worker can race that update. After a
    terminal status is reached the row is never edited again, and neither
    are its `BacktestEquityPoint` / `BacktestTrade` children.

    A FAILED row is a real, kept result, not an error that vanished:
    `error_detail` carries the actual failure (most often
    `InsufficientHistoryError` - the requested window plus indicator warmup
    is not covered by ingested bars) and the run stays queryable alongside
    the successful ones. That is `run_backfill_job`'s posture, deliberately
    NOT D025's engine, which raises because it has nothing to persist either
    way. See `apps/api/app/backtesting/engine_v2.py`.

    `requested_by_user_id` is ON DELETE SET NULL, matching
    `orders.submitted_by_user_id` and
    `market_data_backfill_jobs.requested_by_user_id`: what was run, over
    what window, with what outcome outlives the deletion of whoever asked
    for it.

    Every metric column is nullable because a FAILED run computed none of
    them - a zero would be a fabricated figure, and this schema never writes
    one (docs/TRADING_SAFETY.md's no-fabrication rule).
    """

    __tablename__ = "backtest_runs"
    __table_args__ = (
        Index("ix_backtest_runs_version_created", "strategy_version_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    bar_interval: Mapped[str] = mapped_column(String(8), nullable=False, default="1d")
    """Plain string, not a Postgres ENUM - the same reasoning
    `market_data_bars.bar_interval` documents, and it names the same
    vocabulary, since this is the interval the bars were read at."""
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    """The REQUESTED window. The equity curve may legitimately contain fewer
    days than this range spans - weekends and market holidays are real gaps
    in real bar data and are never filled in with an invented bar."""
    starting_cash: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    status: Mapped[BacktestRunStatus] = mapped_column(
        _pg_enum(BacktestRunStatus, "backtestrunstatus"), nullable=False
    )
    final_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    total_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    win_rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    num_trades: Mapped[int | None] = mapped_column(Integer)
    """Completed round trips, not raw fills - the same definition
    `BacktestResult.num_trades` documents, so that this number and
    `win_rate_pct` stay consistent with each other."""
    error_detail: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the run reached a terminal status. NULL means it never did -
    deliberately not defaulted to `created_at`, the same reasoning
    `orders.reconciled_at` documents."""


class BacktestEquityPoint(Base):
    """One day's mark-to-market equity within one `BacktestRun` (Phase 55,
    migration 0018). A child table rather than a JSON column on the run, for
    the reason `PortfolioSnapshotPositionRow` already documents: a per-day
    time series belongs in rows, where "this run's curve between these two
    dates" is an indexed query rather than a JSON-path scan.

    `(backtest_run_id, date)` is UNIQUE - one point per day per run. Written
    once, in a single bulk insert, when the run reaches SUCCEEDED, and never
    updated afterwards.

    There is one row per BAR in the requested window, not per calendar day:
    a weekend or a market holiday simply has no row, because no bar exists
    for it and interpolating one would fabricate a portfolio value that
    never happened.

    `backtest_run_id` is ON DELETE CASCADE (unlike the run's own RESTRICT
    reference upward to its version): these points have no meaning apart
    from the run that computed them, exactly like `watchlist_items`.
    """

    __tablename__ = "backtest_equity_points"
    __table_args__ = (
        UniqueConstraint("backtest_run_id", "date", name="uq_backtest_equity_point_run_date"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    backtest_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)


class BacktestTrade(Base):
    """One completed round trip within one `BacktestRun` (Phase 55,
    migration 0018) - a buy that opened a flat position followed by the sell
    that closed it back to zero, which is the same unit `RoundTrip` and
    `BacktestResult.num_trades` already count.

    Written once, in a single bulk insert, when the run reaches SUCCEEDED;
    never updated, exactly like `BacktestEquityPoint`. `backtest_run_id` is
    ON DELETE CASCADE for the same reason.

    `side` is a plain `String(8)` holding a `risk.models.Side` VALUE
    ("buy"/"sell"), deliberately NOT the `side` Postgres enum type
    `orders.side` uses. This column is only ever displayed; it is not
    branched on, aggregated by, or constrained against, so the enum type
    would buy nothing here while coupling this table to a type the order
    path owns. See `apps/api/app/backtesting/engine_v2.py` for the current,
    documented scope limit: this phase's engine only ever opens long, so
    every row written today carries "buy".

    `exit_date` / `exit_price` / `return_pct` are nullable to leave room for
    a position still open at the end of the window. Phase 55's engine only
    ever writes CLOSED round trips, so they are in practice always set -
    nullable rather than NOT NULL so that recording an open position later
    is an addition, not a migration that reshapes a table results already
    depend on.
    """

    __tablename__ = "backtest_trades"
    __table_args__ = (Index("ix_backtest_trades_run", "backtest_run_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    backtest_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    exit_date: Mapped[date | None] = mapped_column(Date)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    """(exit_price - entry_price) / entry_price * 100, computed once at
    write time from this row's own two prices. Stored rather than derived on
    read so a result is reproducible from the row alone."""


class WalkForwardRunStatus(str, enum.Enum):  # noqa: UP042 (str mixin kept for SQLAlchemy Enum interop)
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WalkForwardRun(Base):
    """Sequential out-of-sample consistency testing over one
    `StrategyVersion` (Phase 57, migration 0019) - the SAME fixed,
    validated definition replayed across several non-overlapping historical
    windows, to see whether its performance holds up across periods rather
    than being an artifact of the one window an ordinary backtest happened
    to cover.

    Deliberately NOT "walk-forward optimization": this platform has no
    parameter-fitting step (a `StrategyDefinition`'s rules are fixed by
    whoever authored it, D071), so there is nothing to re-fit between
    windows. See `apps/api/app/backtesting/walk_forward.py`'s module
    docstring for the same distinction stated at the point it matters most.

    `strategy_version_id` is `ON DELETE RESTRICT`, matching
    `BacktestRun.strategy_version_id` (D072) for the identical reason: a
    persisted result's exact input must never be able to disappear.

    Every window starts fresh at the same `starting_cash` rather than
    compounding through a running balance - this asks "does this strategy
    perform similarly across different periods," not "what would
    compounding through all of them produce" (a single ordinary backtest
    over the full range already answers the second question).

    Every aggregate column is nullable for the same no-fabrication reason
    every `BacktestRun` metric column is: a `FAILED` run (fewer than two
    complete windows fit in the requested range) computed none of them, and
    `NULL` - never `0` - is what "not computed" means. `num_windows` counts
    every window attempted, including any that individually failed;
    `num_succeeded_windows`/`num_profitable_windows` are always
    `<= num_windows`, and the aggregate statistics are computed only over
    the succeeded subset.
    """

    __tablename__ = "walk_forward_runs"
    __table_args__ = (
        Index("ix_walk_forward_runs_version_created", "strategy_version_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    bar_interval: Mapped[str] = mapped_column(String(8), nullable=False, default="1d")
    overall_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    overall_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False)
    starting_cash: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    status: Mapped[WalkForwardRunStatus] = mapped_column(
        _pg_enum(WalkForwardRunStatus, "walkforwardrunstatus"), nullable=False
    )
    num_windows: Mapped[int | None] = mapped_column(Integer)
    num_succeeded_windows: Mapped[int | None] = mapped_column(Integer)
    num_profitable_windows: Mapped[int | None] = mapped_column(Integer)
    mean_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    stddev_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    best_window_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    worst_window_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    error_detail: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WalkForwardWindow(Base):
    """One sequential window of a `WalkForwardRun` (Phase 57, migration
    0019). `backtest_run_id` points at a REAL, ordinary row in
    `backtest_runs` (migration 0018) - the orchestrator that builds these
    calls `apps/api/app/backtesting/engine_v2.py::run_strategy_backtest()`
    unchanged, once per window, rather than a second execution path that
    could drift from the first.

    `backtest_run_id` is `ON DELETE RESTRICT` for the same reason the
    parent run's own `strategy_version_id` is: a window's recorded result
    must not be able to vanish either. `walk_forward_run_id` is
    `ON DELETE CASCADE` - a window has no meaning apart from the run that
    requested it, the same relationship `BacktestEquityPoint` has to
    `BacktestRun`.
    """

    __tablename__ = "walk_forward_windows"
    __table_args__ = (
        UniqueConstraint(
            "walk_forward_run_id", "window_index", name="uq_walk_forward_window_run_index"
        ),
        Index("ix_walk_forward_windows_run", "walk_forward_run_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    walk_forward_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("walk_forward_runs.id", ondelete="CASCADE"), nullable=False
    )
    window_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    backtest_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="RESTRICT"), nullable=False
    )


class MonteCarloRunStatus(str, enum.Enum):  # noqa: UP042 (str mixin kept for SQLAlchemy Enum interop)
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MonteCarloRun(Base):
    """Seeded bootstrap resampling of one completed `BacktestRun`'s trade
    returns into a distribution of simulated outcomes (Phase 57, migration
    0020). Answers a different question than the backtest it's built from:
    not "what happened," but "how much would the result have varied by
    chance alone if these same trade outcomes had occurred in a different
    order (with replacement - some repeated, some never occurring)?" - a
    statement about sequence risk already latent in the trades, not a claim
    about a different or better trading history.

    `backtest_run_id` is `ON DELETE RESTRICT` and is the ONLY foreign key
    this table needs - a Monte Carlo run's entire input is one existing
    `BacktestRun`'s trade list, which already pins its own
    `strategy_version_id` provenance.

    Only aggregate percentile statistics are persisted, never every
    simulated path - with 1,000+ simulations each holding a full equity
    curve, storing every path would dwarf every other table in this schema
    for comparatively little benefit. `random_seed` alone is sufficient to
    regenerate the full distribution later, since resampling is
    deterministic given a seed and the same input trade list - stored as
    `BigInteger` rather than this schema's usual `Integer`, since a seed is
    drawn from a wide range specifically so it is not a predictable or
    guessable value.

    Every statistic column is nullable for the same no-fabrication reason
    every other persisted-result metric column in this schema is: a
    `FAILED` run (too few trades in the source backtest to resample
    meaningfully) computed none of them, and `NULL`, never `0`, is what
    that means.
    """

    __tablename__ = "monte_carlo_runs"
    __table_args__ = (
        Index("ix_monte_carlo_runs_backtest_created", "backtest_run_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    backtest_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    num_simulations: Mapped[int] = mapped_column(Integer, nullable=False)
    random_seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[MonteCarloRunStatus] = mapped_column(
        _pg_enum(MonteCarloRunStatus, "montecarlorunstatus"), nullable=False
    )
    num_trades_resampled: Mapped[int | None] = mapped_column(Integer)
    median_final_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    p5_final_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    p95_final_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    median_max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    p5_max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    p95_max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    probability_of_ruin_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    error_detail: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RobustnessRunStatus(str, enum.Enum):  # noqa: UP042 (str mixin kept for SQLAlchemy Enum interop)
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RobustnessRun(Base):
    """Parameter-sensitivity testing of one `StrategyVersion` (Phase 58,
    migration 0021): the SAME window replayed once with the definition
    exactly as authored (the baseline) and once more per parameter nudged
    +/- `magnitude_pct`, so that "this strategy returned X%" can be checked
    against "and it still roughly does when its numbers are slightly
    different" rather than against one exact parameter set that may simply
    have suited this history.

    Deliberately NOT an optimizer, and NOT a search. Nothing here looks for
    a BETTER parameter set, ranks perturbed variants against the baseline as
    candidates, or writes any variant back to a `StrategyVersion` - a
    definition's rules are fixed by whoever authored them (D071), and every
    perturbed definition built during a run lives only in memory for the
    length of the replay that measures it. What this table records is
    sensitivity, which is a question about the ORIGINAL definition.

    `strategy_version_id` is `ON DELETE RESTRICT`, matching
    `WalkForwardRun.strategy_version_id` and `BacktestRun`'s before it (D072)
    for the identical reason: a persisted result's exact input must never be
    able to disappear. Which parameters a run perturbed, and by how much,
    only means anything next to the definition it perturbed them FROM.

    Every perturbation starts fresh at the same `starting_cash` over the same
    `[start_date, end_date]` window as the baseline - the whole point is that
    the only difference between two of these replays is the one parameter, so
    a differing window or capital base would confound exactly the comparison
    being made.

    Every metric column is nullable for the same no-fabrication reason every
    `BacktestRun` and `WalkForwardRun` metric column is: a `FAILED` run (a
    definition with no numeric parameters to perturb, or a baseline that
    could not itself be replayed) computed none of them, and `NULL` - never
    `0` - is what "not computed" means. `num_perturbations` counts every
    perturbation ATTEMPTED, including any whose own replay failed;
    `num_succeeded_perturbations` is always `<= num_perturbations`, and the
    aggregate statistics are computed only over the succeeded subset.

    `max_return_deviation_pct` is the largest absolute gap between a
    succeeded perturbation's return and the baseline's, and it is
    deliberately ONE SIMPLE HONEST NUMBER rather than a composite "robustness
    score". This phase does not attempt to define one weighted figure across
    return, drawdown and whatever else - any such weighting encodes a risk
    preference nobody here has stated, and a single 0-100 "score" invites
    being read as an authoritative verdict on a strategy. A future phase can
    build a scoring model ON TOP of these raw numbers if one is ever wanted;
    it cannot recover the raw numbers from a score.
    """

    __tablename__ = "robustness_runs"
    __table_args__ = (
        Index("ix_robustness_runs_version_created", "strategy_version_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    bar_interval: Mapped[str] = mapped_column(String(8), nullable=False, default="1d")
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    starting_cash: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    magnitude_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    """How far each parameter was nudged, as a percentage of its own original
    value - `10.0000` means every perturbed variant moved one parameter by
    +10% or -10%. Stored rather than assumed so a run's numbers stay
    interpretable if the request default ever changes."""
    status: Mapped[RobustnessRunStatus] = mapped_column(
        _pg_enum(RobustnessRunStatus, "robustnessrunstatus"), nullable=False
    )
    baseline_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    baseline_max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    num_perturbations: Mapped[int | None] = mapped_column(Integer)
    num_succeeded_perturbations: Mapped[int | None] = mapped_column(Integer)
    mean_perturbed_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    stddev_perturbed_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    max_return_deviation_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    error_detail: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RobustnessPerturbationResult(Base):
    """One perturbed variant of a `RobustnessRun`'s definition and what
    replaying it produced (Phase 58, migration 0021). Exactly one parameter
    differs from the baseline definition - `parameter_path` names which,
    `original_value` and `perturbed_value` say what it went from and to.

    `robustness_run_id` is `ON DELETE CASCADE`: a perturbation result has no
    meaning apart from the run that requested it, the same relationship
    `WalkForwardWindow` has to `WalkForwardRun` and `BacktestEquityPoint` has
    to `BacktestRun`. There is deliberately no FK to a `BacktestRun` here,
    which is where this table differs from `WalkForwardWindow`: a
    perturbation is replayed through `engine_v2`'s pure, no-persistence
    helpers rather than through `run_strategy_backtest`, because it runs a
    definition that no `StrategyVersion` contains - and a `backtest_runs` row
    is a record OF a version's definition. Writing one anyway would mean
    persisting a backtest of rules nobody authored, filed under a version
    whose definition says something else.

    `clamped` records that the generator could not apply the full nudge and
    had to stop at a parameter's own legal bound (an indicator period cannot
    go below 1, a fraction cannot exceed 1). A clamped variant is still a
    real measurement of a real definition, so it is replayed and reported
    like any other - but the flag is what tells a reader that this row's
    effective magnitude was smaller than the run's `magnitude_pct`.

    `status` is a plain `String(16)`, not a Postgres enum type. Same
    precedent `BacktestTrade.side` set: this value is display-only and is
    never branched on outside this row, so a dedicated type (and the
    migration needed to ever extend it) buys nothing here. The run-level
    `status` IS an enum because the orchestrator and the routes both branch
    on it.

    `total_return_pct` / `max_drawdown_pct` are nullable for the reason every
    metric column in this schema is: a `failed` perturbation (most often a
    much larger perturbed indicator period needing more warmup history than
    the bar store holds) computed neither of them, `error_detail` says why,
    and `NULL` - never `0` - is what "not computed" means. A failed
    perturbation contributes nothing to the parent run's aggregates rather
    than contributing a zero to them.
    """

    __tablename__ = "robustness_perturbations"
    __table_args__ = (Index("ix_robustness_perturbations_run", "robustness_run_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    robustness_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("robustness_runs.id", ondelete="CASCADE"), nullable=False
    )
    parameter_path: Mapped[str] = mapped_column(String(128), nullable=False)
    direction: Mapped[str] = mapped_column(String(4), nullable=False)
    original_value: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    perturbed_value: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    clamped: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    total_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    error_detail: Mapped[str | None] = mapped_column(String(500))


class UniverseScanStatus(str, enum.Enum):  # noqa: UP042 (str mixin kept for SQLAlchemy Enum interop)
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class UniverseScan(Base):
    """One validated `StrategyVersion` run across MANY symbols over the same
    window, so the symbols can be ranked against each other (Phase 60,
    migration 0022) - "which of these markets does this strategy actually
    suit", where a `BacktestRun` answers "how did it do on this one".

    **Every symbol is a REAL, persisted `BacktestRun`.** The orchestrator
    (`apps/api/app/backtesting/universe_scan.py`) calls
    `engine_v2.run_strategy_backtest` unchanged, once per symbol, exactly as
    `WalkForwardRun` calls it once per window - same risk engine, same
    portfolio manager, same bar store, same persistence. That is right here
    for the reason it was right there and NOT right for
    `RobustnessPerturbationResult`: a scanned symbol replays the version's
    OWN definition, unaltered, so a `backtest_runs` row filed under that
    version is an accurate record of what ran. Only a perturbed definition -
    rules no version contains - has to stay out of that table.

    `strategy_version_id` is `ON DELETE RESTRICT`, the same reasoning
    `BacktestRun`, `WalkForwardRun` and `RobustnessRun` all give: a persisted
    result's exact input must never be able to disappear underneath it. A
    ranking of twenty symbols means nothing without the definition they were
    ranked under. `requested_by_user_id` is `ON DELETE SET NULL`, matching
    every sibling: what was scanned, over what window, with what outcome
    outlives the deletion of whoever asked for it.

    `scan_mode` is a plain `String(16)` (`"explicit_list"` / `"all_ingested"`)
    rather than a second Postgres enum type - the precedent
    `BacktestTrade.side` and `RobustnessPerturbationResult.status` set: this
    value is display-and-provenance only and is never branched on outside its
    own row. The run-level `status` IS an enum because the orchestrator and
    the routes both branch on it.

    **`requested_symbols` is the first `ARRAY` column in this schema since
    `Role.permissions`**, and is used for the same kind of reason: a short,
    flat, read-as-a-whole list of strings that is never joined against or
    filtered by, which a child table would model at the cost of a join that
    buys nothing. It is the EXACT list the caller asked for, normalized -
    provenance, not a result. The list actually scanned is what
    `num_symbols` counts and what the `universe_scan_results` rows enumerate;
    the two can differ in principle, and keeping the request verbatim is what
    makes that visible. It is NULL - not an empty array - when
    `scan_mode == "all_ingested"`, because the caller named no symbols at
    all, and `{}` would read as "asked for none".

    Every aggregate is nullable for the same no-fabrication reason every
    metric column in this schema is (docs/TRADING_SAFETY.md): a FAILED scan
    counted none of them, and `NULL` - never `0` - is what "not computed"
    means. `num_symbols` counts every symbol ATTEMPTED, `num_succeeded`
    those whose own backtest reached SUCCEEDED, and both stay real
    observations even when they are 0.

    **`num_qualified` counts SUCCEEDED symbols with `total_return_pct > 0`** -
    that is, "profitable over this exact window", and nothing more. It is a
    deliberately simple first-pass filter chosen so its meaning is fully
    stated by that one sentence, in the same spirit as D075's scoring
    thresholds and D076's leaderboard tiers: a stated, adjustable heuristic
    rather than an unexplained composite. It says nothing about risk-adjusted
    return, drawdown, trade count or statistical significance, and it is not
    a recommendation to trade anything. A later phase can define a richer
    qualification rule over these same raw columns; it could not recover the
    raw columns from a score.
    """

    __tablename__ = "universe_scans"
    __table_args__ = (
        Index("ix_universe_scans_version_created", "strategy_version_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    bar_interval: Mapped[str] = mapped_column(String(8), nullable=False, default="1d")
    """Plain string, not a Postgres ENUM - the same reasoning
    `market_data_bars.bar_interval` documents, and it names the same
    vocabulary, since this is the interval every scanned symbol's bars were
    read at."""
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    """The REQUESTED window, applied identically to every symbol - a ranking
    whose rows covered different periods would not be a ranking."""
    starting_cash: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    """Every symbol starts fresh at this same figure; they do not share or
    compound a balance. The same reasoning `WalkForwardRun` gives for its
    windows - these are parallel measurements to be compared, not one
    portfolio traded across many symbols."""
    scan_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_symbols: Mapped[list[str] | None] = mapped_column(ARRAY(String(32)))
    status: Mapped[UniverseScanStatus] = mapped_column(
        _pg_enum(UniverseScanStatus, "universescanstatus"), nullable=False
    )
    num_symbols: Mapped[int | None] = mapped_column(Integer)
    num_succeeded: Mapped[int | None] = mapped_column(Integer)
    num_qualified: Mapped[int | None] = mapped_column(Integer)
    error_detail: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UniverseScanResult(Base):
    """One symbol's outcome within one `UniverseScan` (Phase 60, migration
    0022) - a thin index row over the real `BacktestRun` that produced it.

    `backtest_run_id` points at a REAL, ordinary row in `backtest_runs`,
    exactly as `WalkForwardWindow.backtest_run_id` does, and is
    `ON DELETE RESTRICT` for the same reason the parent scan's own
    `strategy_version_id` is: a recorded result must not be able to vanish
    from under the row that reports it. `universe_scan_id` is
    `ON DELETE CASCADE` - a per-symbol result has no meaning apart from the
    scan that requested it, the same relationship `WalkForwardWindow` has to
    `WalkForwardRun` and `BacktestEquityPoint` has to `BacktestRun`.

    The four metric columns are COPIED from the linked run rather than being
    read through the FK on every request. That is denormalization with a
    purpose: ranking twenty symbols is then one indexed read of this table
    instead of twenty joins, and the copy can never go stale because a
    terminal `BacktestRun` is never edited again (see `BacktestRun`'s
    append-only note). `backtest_run_id` remains the handle for everything
    NOT copied - the equity curve and the trade list are one
    `GET /backtest-runs/{id}` away, so none of it is duplicated here.

    `status` is a plain `String(16)` (`"succeeded"` / `"failed"`) mirroring
    the linked run's own status, not a second Postgres enum type - the same
    precedent `BacktestTrade.side` and `RobustnessPerturbationResult.status`
    set, and the same reason: display-only, never branched on outside this
    row.

    Every metric is nullable because a FAILED per-symbol backtest computed
    none of them - most often a symbol with no ingested bars over this
    window, which is a normal, recorded outcome of scanning a list someone
    typed. `error_detail` says why, `NULL` means "not computed" and never
    `0`, and the failed row still exists so the gap is visible rather than
    silently dropped from the universe.

    There is deliberately no `rank` column: rank is computed on READ, over
    the SUCCEEDED rows by `total_return_pct` descending, exactly as D076's
    leaderboard ranks on read. A stored rank would be a second copy of an
    ordering already fully determined by the data, and any later change to
    how the ranking is defined would silently disagree with every row
    written under the old rule.
    """

    __tablename__ = "universe_scan_results"
    __table_args__ = (
        UniqueConstraint(
            "universe_scan_id", "symbol", name="uq_universe_scan_result_scan_symbol"
        ),
        Index("ix_universe_scan_results_scan", "universe_scan_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    universe_scan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("universe_scans.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    backtest_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    total_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    win_rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    num_trades: Mapped[int | None] = mapped_column(Integer)
    error_detail: Mapped[str | None] = mapped_column(String(500))
