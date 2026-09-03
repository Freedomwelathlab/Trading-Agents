"""orders: broker_order_id / broker_status / reconciled_at, plus the two
new orderstatus labels that make an accepted-but-unexecuted live order
recordable and later resolvable (docs/DECISIONS.md D066)

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-03

WHY THE ENUM VALUES ARE ADDED IN THEIR OWN TRANSACTION
------------------------------------------------------
Postgres refuses to use a value added by `ALTER TYPE ... ADD VALUE` inside
the SAME transaction that added it ("unsafe use of new value of enum
type"). Alembic runs a migration in one transaction by default, and the
partial index below has `submitted_unconfirmed` as a literal in its
predicate - so the label must be committed first. `op.execute("COMMIT")`
before the ADD VALUEs is the standard way to do that and is what makes this
migration work on a real server rather than only in a test that never
exercises the index.

That does mean this migration is not atomic. It is safe anyway because
every step is additive and independently idempotent-in-effect: two new enum
labels nothing yet writes, three nullable columns, and one index. A failure
part-way leaves a schema that is a strict subset of the target and that the
pre-Phase-49 code still runs against unchanged.

WHY EVERY COLUMN IS NULLABLE, WITH NO BACKFILL
----------------------------------------------
Every existing `orders` row was written by the paper path or by a live path
that never recorded a broker id. There is no true value to backfill for any
of them, and the no-fabrication rule (spec Sec57) means the honest schema is
one where "we do not have this" is representable. Contrast D044's
`cost_basis_method`, which WAS backfilled - there the historical value was
genuinely known.

THE UNIQUE CONSTRAINT ON broker_order_id
----------------------------------------
Two rows claiming the same venue order would make the audit trail ambiguous
about something real money moved through, and it would let a repeated
reconciliation double-count a fill. Postgres treats NULLs as distinct in a
unique index, so the constraint costs the (many) paper rows nothing.

THE PARTIAL INDEX
-----------------
`ix_orders_unconfirmed` covers only rows in `submitted_unconfirmed`. The
reconciler polls "every unresolved live order" on an interval forever, and
`orders` is an append-only table that only grows; without this the poll
degrades into a repeated full scan. Partial rather than full because the
set it needs to find is normally empty or tiny.

DOWNGRADE
---------
The columns and the index drop cleanly. The two enum LABELS cannot be
dropped - Postgres has no `ALTER TYPE ... DROP VALUE` - so the downgrade
recreates the `orderstatus` type with only the original two labels, after
first refusing to run if any row actually uses one of the new ones.
Refusing is deliberate: silently rewriting a real, broker-confirmed order
to `rejected` (or deleting it) to make a downgrade succeed would destroy
exactly the audit record this migration exists to create.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # See the module docstring: the new labels must be committed before the
    # partial index below can reference one of them as a literal.
    op.execute("COMMIT")
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'submitted_unconfirmed'")
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'broker_closed_unfilled'")

    op.add_column("orders", sa.Column("broker_order_id", sa.String(length=64), nullable=True))
    op.add_column("orders", sa.Column("broker_status", sa.String(length=32), nullable=True))
    op.add_column(
        "orders", sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_unique_constraint("uq_orders_broker_order_id", "orders", ["broker_order_id"])

    op.create_index(
        "ix_orders_unconfirmed",
        "orders",
        ["broker_id", "submitted_at"],
        postgresql_where=sa.text("status = 'submitted_unconfirmed'"),
    )


def downgrade() -> None:
    op.drop_index("ix_orders_unconfirmed", table_name="orders")
    op.drop_constraint("uq_orders_broker_order_id", "orders", type_="unique")
    op.drop_column("orders", "reconciled_at")
    op.drop_column("orders", "broker_status")
    op.drop_column("orders", "broker_order_id")

    bind = op.get_bind()
    in_use = bind.execute(
        sa.text(
            "SELECT count(*) FROM orders WHERE status IN "
            "('submitted_unconfirmed', 'broker_closed_unfilled')"
        )
    ).scalar_one()
    if in_use:
        raise RuntimeError(
            f"Refusing to downgrade: {in_use} orders row(s) still use a Phase 49 status. "
            "Downgrading would have to rewrite or delete a real, broker-confirmed audit "
            "record to fit the old two-label enum, and destroying that record is exactly "
            "what migration 0014 exists to prevent (docs/DECISIONS.md D066). Resolve or "
            "archive those rows deliberately first."
        )

    # No ALTER TYPE ... DROP VALUE exists, so the type is rebuilt. Safe only
    # because the check above proved no row uses a label being removed.
    op.execute("ALTER TYPE orderstatus RENAME TO orderstatus_old")
    op.execute("CREATE TYPE orderstatus AS ENUM ('filled', 'rejected')")
    op.execute(
        "ALTER TABLE orders ALTER COLUMN status TYPE orderstatus "
        "USING status::text::orderstatus"
    )
    op.execute("DROP TYPE orderstatus_old")
