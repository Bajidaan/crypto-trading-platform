"""Canonical domain enumerations for CEX and future DEX trading."""

from enum import StrEnum


class ExchangeId(StrEnum):
    """Supported exchanges for Phase 1 CEX Engine."""

    BINANCE = "BINANCE"
    BYBIT = "BYBIT"


class MarketType(StrEnum):
    """Supported market types."""

    SPOT = "SPOT"
    FUTURES_USDT = "FUTURES_USDT"


class ExecutionMode(StrEnum):
    """Execution mode for strategies, positions, and orders."""

    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class OrderSide(StrEnum):
    """Order side on the exchange book."""

    BUY = "BUY"
    SELL = "SELL"


class PositionSide(StrEnum):
    """Directional exposure of a position."""

    LONG = "LONG"
    SHORT = "SHORT"


class MarginMode(StrEnum):
    """Futures margin isolation mode."""

    ISOLATED = "ISOLATED"
    CROSSED = "CROSSED"
    CROSS = "CROSSED"


class OrderType(StrEnum):
    """Normalized order types across exchanges."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    STOP_LIMIT = "STOP_LIMIT"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    TAKE_PROFIT_LIMIT = "TAKE_PROFIT_LIMIT"


class OrderPurpose(StrEnum):
    """Business purpose of an order within a position's lifecycle."""

    ENTRY = "ENTRY"
    DCA_SAFETY = "DCA_SAFETY"
    HARD_STOP_LOSS = "HARD_STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    EMERGENCY_CLOSE = "EMERGENCY_CLOSE"


class OrderStatus(StrEnum):
    """Deterministic order state machine states."""

    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    UNKNOWN_SYNCING = "UNKNOWN_SYNCING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    CANCELED_PARTIAL = "CANCELED_PARTIAL"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    FAILED_SUBMIT = "FAILED_SUBMIT"


class PositionStatus(StrEnum):
    """Position lifecycle states."""

    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    RECONCILED_STALE = "RECONCILED_STALE"


class SignalSource(StrEnum):
    """Origin of a trading signal."""

    STRATEGY = "STRATEGY"
    WEBHOOK = "WEBHOOK"
    MANUAL = "MANUAL"


class SignalStatus(StrEnum):
    """Lifecycle status of an emitted trade signal."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED_RISK = "REJECTED_RISK"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
