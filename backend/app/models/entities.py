"""SQLAlchemy 2.0 ORM entities implementing the Stage 2 Database ERD."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
import uuid
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import (
    ExchangeId,
    ExecutionMode,
    MarginMode,
    MarketType,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    SignalSource,
    SignalStatus,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid_str() -> str:
    return str(uuid.uuid4())


class ExchangeConnection(Base):
    """Encrypted exchange API connection and capital allocation limits."""

    __tablename__ = "exchange_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    exchange_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    api_key_masked: Mapped[str] = mapped_column(String(64), nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    api_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    passphrase_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_testnet: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    spot_capital_cap_usd: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), default=Decimal("1000.0"), nullable=False
    )
    futures_capital_cap_usd: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), default=Decimal("1000.0"), nullable=False
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False
    )

    strategies: Mapped[list["StrategyConfig"]] = relationship(
        back_populates="exchange_connection", cascade="all, delete-orphan"
    )
    positions: Mapped[list["Position"]] = relationship(back_populates="exchange_connection")


class StrategyConfig(Base):
    """Configured trading strategy instance (Built-in, Custom Plugin, or Webhook target)."""

    __tablename__ = "strategies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    strategy_class: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    execution_mode: Mapped[str] = mapped_column(
        String(20), default=ExecutionMode.PAPER.value, nullable=False
    )
    exchange_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("exchange_connections.id", ondelete="SET NULL"), nullable=True
    )
    market_type: Mapped[str] = mapped_column(
        String(24), default=MarketType.SPOT.value, nullable=False
    )
    symbols: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), default="15m", nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    risk_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False
    )

    exchange_connection: Mapped["ExchangeConnection | None"] = relationship(back_populates="strategies")
    signals: Mapped[list["TradeSignalRecord"]] = relationship(
        back_populates="strategy", cascade="all, delete-orphan"
    )
    positions: Mapped[list["Position"]] = relationship(back_populates="strategy")


class TradeSignalRecord(Base):
    """Persisted record of every generated or received trading signal and its risk gate verdict."""

    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    strategy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(24), default=SignalSource.STRATEGY.value, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    market_type: Mapped[str] = mapped_column(String(24), nullable=False)
    side: Mapped[str] = mapped_column(String(12), nullable=False)
    signal_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    stop_loss_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    take_profit_targets: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=SignalStatus.PENDING.value, nullable=False, index=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, nullable=False)

    strategy: Mapped["StrategyConfig | None"] = relationship(back_populates="signals")
    position: Mapped["Position | None"] = relationship(back_populates="signal", uselist=False)


class Position(Base):
    """Active or historical trading position across Spot or USDT-M Perpetual Futures."""

    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    strategy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    exchange_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("exchange_connections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    signal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("signals.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    execution_mode: Mapped[str] = mapped_column(
        String(20), default=ExecutionMode.PAPER.value, nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    market_type: Mapped[str] = mapped_column(String(24), nullable=False)
    side: Mapped[str] = mapped_column(String(12), default=PositionSide.LONG.value, nullable=False)
    margin_mode: Mapped[str] = mapped_column(
        String(16), default=MarginMode.ISOLATED.value, nullable=False
    )
    leverage: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    initial_qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    current_qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    stop_loss_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    active_stop_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stop_order_live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trailing_stop_pct: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    highest_or_lowest_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    take_profit_state: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    realized_pnl_usd: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), default=Decimal("0"), nullable=False
    )
    unrealized_pnl_usd: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), default=Decimal("0"), nullable=False
    )
    total_fees_usd: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), default=Decimal("0"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), default=PositionStatus.OPEN.value, nullable=False, index=True
    )
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False
    )

    strategy: Mapped["StrategyConfig | None"] = relationship(back_populates="positions")
    exchange_connection: Mapped["ExchangeConnection | None"] = relationship(back_populates="positions")
    signal: Mapped["TradeSignalRecord | None"] = relationship(back_populates="position")
    orders: Mapped[list["Order"]] = relationship(
        back_populates="position", cascade="all, delete-orphan"
    )


class Order(Base):
    """Order entity with deterministic client_order_id idempotency protection."""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    position_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("positions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_order_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[str] = mapped_column(String(24), nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(32), default=OrderPurpose.ENTRY.value, nullable=False
    )
    order_type: Mapped[str] = mapped_column(
        String(24), default=OrderType.MARKET.value, nullable=False
    )
    side: Mapped[str] = mapped_column(String(12), nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    requested_qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    filled_qty: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), default=Decimal("0"), nullable=False
    )
    requested_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    stop_trigger_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    fee_usd: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), default=Decimal("0"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), default=OrderStatus.PENDING_SUBMIT.value, nullable=False, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False
    )

    position: Mapped["Position"] = relationship(back_populates="orders")
    fills: Mapped[list["Fill"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class Fill(Base):
    """Individual trade execution fill linked to an Order."""

    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint("exchange_trade_id", name="uq_fills_exchange_trade_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    exchange_trade_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    fill_qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), default=Decimal("0"), nullable=False
    )
    fee_currency: Mapped[str] = mapped_column(String(16), default="USDT", nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, nullable=False)

    order: Mapped["Order"] = relationship(back_populates="fills")


class OhlcvCandle(Base):
    """Historical and cached candlestick bars for backtesting and indicator warm-up."""

    __tablename__ = "ohlcv_candles"
    __table_args__ = (
        Index(
            "ix_ohlcv_lookup",
            "exchange_id",
            "symbol",
            "market_type",
            "timeframe",
            "open_time",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange_id: Mapped[str] = mapped_column(String(24), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[str] = mapped_column(String(24), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)


class BacktestRun(Base):
    """Stored results and equity curve of a historical backtest execution."""

    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    strategy_class: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange_id: Mapped[str] = mapped_column(String(24), default=ExchangeId.BINANCE.value, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[str] = mapped_column(String(24), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    initial_capital_usd: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    equity_curve: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    trades_log: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, nullable=False)


class GlobalRiskState(Base):
    """Singleton row (id=1) holding global risk circuit breakers, pause state, and kill switch."""

    __tablename__ = "global_risk_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_concurrent_positions: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    max_daily_drawdown_pct: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal("5.0"), nullable=False
    )
    max_leverage_cap: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    default_risk_per_trade_pct: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal("1.0"), nullable=False
    )
    compounding_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False
    )


class AuditLog(Base):
    """Immutable financial & operational audit trail."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    category: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="INFO", nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, nullable=False, index=True)
