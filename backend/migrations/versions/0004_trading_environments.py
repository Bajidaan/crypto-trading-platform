"""Demo and live trading environments replace the paper simulator

- exchange_connections.environment (DEMO | LIVE) replaces is_testnet. "Testnet" keys are
  treated as Binance Demo Trading keys.
- global_risk_state.active_environment holds the environment new trades go to.
  It starts on DEMO.

Paper positions and orders from the removed simulator are left in place as history.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("exchange_connections") as batch:
        batch.add_column(sa.Column("environment", sa.String(length=8), nullable=False, server_default="DEMO"))
    op.execute(
        "UPDATE exchange_connections SET environment = CASE WHEN is_testnet THEN 'DEMO' ELSE 'LIVE' END"
    )
    with op.batch_alter_table("exchange_connections") as batch:
        batch.drop_column("is_testnet")
        batch.create_index("ix_exchange_connections_environment", ["environment"])

    with op.batch_alter_table("global_risk_state") as batch:
        batch.add_column(
            sa.Column("active_environment", sa.String(length=8), nullable=False, server_default="DEMO")
        )


def downgrade() -> None:
    with op.batch_alter_table("global_risk_state") as batch:
        batch.drop_column("active_environment")

    with op.batch_alter_table("exchange_connections") as batch:
        batch.add_column(sa.Column("is_testnet", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE exchange_connections SET is_testnet = (environment = 'DEMO')")
    with op.batch_alter_table("exchange_connections") as batch:
        batch.drop_index("ix_exchange_connections_environment")
        batch.drop_column("environment")
