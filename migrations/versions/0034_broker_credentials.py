"""per-broker encrypted API credentials (Phase 90, D109)

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-23

One row per broker, holding that venue's API credentials as a single
Fernet token. Until now the platform's only live credentials came from
process environment variables, which express exactly one broker per
deployment; this is what lets an IBKR account, an IG account and a Kraken
account coexist and belong to different broker rows.

**`ciphertext` is the whole credential set, encrypted as one JSON object.**
Not a column per field: the set of fields differs per provider (IG needs a
username and an account type, Kraken needs a key pair), and a table whose
columns are the union of every venue's fields is mostly nulls and needs a
migration every time a venue is added.

**`field_names` is in the clear, on purpose.** The UI must be able to show
which fields are set without the encryption key, and "an access token is
present" is not a secret. It holds names only — never a value, never a
prefix, never a length.

**`key_fingerprint` is a salted, truncated digest of the encryption key,
not the key.** Its only job is to distinguish "encrypted under the key you
have" from "encrypted under a key you no longer have", so a rotated key
produces an actionable message instead of an opaque decryption failure.

CASCADE on `broker_id`: a deleted broker's credentials must not outlive
it, and a stranded encrypted blob nobody can attribute is worse than no
blob. UNIQUE on `broker_id`: a broker has one credential set, and a second
row would make "which one is live?" a question with no answer.

DOWNGRADE drops the table, and with it every stored credential. They
cannot be recovered from a backup taken before the key was set; re-enter
them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034"
down_revision = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "broker_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "broker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("brokers.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "field_names",
            postgresql.ARRAY(sa.String(64)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("broker_credentials")
