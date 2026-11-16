"""Exact decimal storage on SQLite and a per-order fill key

- SQLite stored every Numeric column as REAL (binary float). Those columns become TEXT so
  ExactDecimal round-trips Decimal values exactly. PostgreSQL already has a real NUMERIC
  and is left unchanged. Values written before this migration were already rounded by
  float storage; they are carried over as-is.
- fills.exchange_trade_id was unique across all symbols, but Binance trade ids are only
  unique per symbol. The key becomes (order_id, exchange_trade_id).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# table -> [(column, precision, scale, nullable)]
DECIMAL_COLUMNS: dict[str, list[tuple[str, int, int, bool]]] = {
    "backtest_runs": [("initial_capital_usd", 28, 12, False)],
    "exchange_connections": [
        ("spot_capital_cap_usd", 28, 12, False),
        ("futures_capital_cap_usd", 28, 12, False),
    ],
    "global_risk_state": [
        ("max_daily_drawdown_pct", 10, 4, False),
        ("default_risk_per_trade_pct", 10, 4, False),
    ],
    "ohlcv_candles": [
        ("open", 28, 12, False),
        ("high", 28, 12, False),
        ("low", 28, 12, False),
        ("close", 28, 12, False),
        ("volume", 28, 12, False),
    ],
    "signals": [("signal_price", 28, 12, True), ("stop_loss_price", 28, 12, False)],
    "positions": [
        ("entry_price", 28, 12, False),
        ("initial_qty", 28, 12, False),
        ("current_qty", 28, 12, False),
        ("stop_loss_price", 28, 12, True),
        ("trailing_stop_pct", 28, 12, True),
        ("highest_or_lowest_price", 28, 12, True),
        ("realized_pnl_usd", 28, 12, False),
        ("unrealized_pnl_usd", 28, 12, False),
        ("total_fees_usd", 28, 12, False),
    ],
    "orders": [
        ("requested_qty", 28, 12, False),
        ("filled_qty", 28, 12, False),
        ("requested_price", 28, 12, True),
        ("stop_trigger_price", 28, 12, True),
        ("avg_fill_price", 28, 12, True),
        ("fee_usd", 28, 12, False),
    ],
    "fills": [
        ("fill_price", 28, 12, False),
        ("fill_qty", 28, 12, False),
        ("fee_amount", 28, 12, False),
    ],
}


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    sqlite = _is_sqlite()
    for table, columns in DECIMAL_COLUMNS.items():
        with op.batch_alter_table(table) as batch:
            if sqlite:
                for name, precision, scale, nullable in columns:
                    batch.alter_column(
                        name,
                        existing_type=sa.Numeric(precision=precision, scale=scale),
                        type_=sa.Text(),
                        existing_nullable=nullable,
                    )
            if table == "fills":
                batch.drop_constraint("uq_fills_exchange_trade_id", type_="unique")
                batch.create_unique_constraint("uq_fills_order_trade", ["order_id", "exchange_trade_id"])


def downgrade() -> None:
    sqlite = _is_sqlite()
    for table, columns in DECIMAL_COLUMNS.items():
        with op.batch_alter_table(table) as batch:
            if table == "fills":
                batch.drop_constraint("uq_fills_order_trade", type_="unique")
                batch.create_unique_constraint("uq_fills_exchange_trade_id", ["exchange_trade_id"])
            if sqlite:
                for name, precision, scale, nullable in columns:
                    batch.alter_column(
                        name,
                        existing_type=sa.Text(),
                        type_=sa.Numeric(precision=precision, scale=scale),
                        existing_nullable=nullable,
                    )
