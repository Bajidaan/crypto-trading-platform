"""Hide strategies without deleting them

Adds strategies.is_hidden. Hidden strategies keep their positions and history but are left
out of the strategy list and cannot be activated.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("strategies", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_hidden", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("strategies", schema=None) as batch_op:
        batch_op.drop_column("is_hidden")
