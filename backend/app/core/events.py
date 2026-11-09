"""In-memory typed asynchronous Event Bus for decoupled intra-process communication."""

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any, Awaitable, Callable
import uuid
from pydantic import BaseModel, Field

from app.models.enums import (
    ExchangeId,
    ExecutionMode,
    MarketType,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    PositionSide,
    PositionStatus,
    SignalSource,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DomainEvent(BaseModel):
    """Base immutable domain event."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    topic: str
    timestamp: datetime = Field(default_factory=_utc_now)


class MarketCandleEvent(DomainEvent):
    """Emitted when a new candlestick updates or closes."""

    topic: str = "market:kline"
    exchange_id: ExchangeId
    symbol: str
    market_type: MarketType
    timeframe: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool = True


class TakeProfitTargetSpec(BaseModel):
    """Specification for a single partial or full Take-Profit level."""

    target_index: int = Field(ge=1)
    price: Decimal
    close_Dynamic_pct: Decimal = Field(
        default=Decimal("100"),
        ge=Decimal("1"),
        le=Decimal("100"),
        description="Percentage of initial position quantity to close at this target",
    )
    move_sl_to_breakeven: bool = False


class TradeSignalEvent(DomainEvent):
    """Standardized signal intent emitted by internal strategies or external webhooks."""

    topic: str = "strategy:signal"
    strategy_id: str
    source: SignalSource
    exchange_id: ExchangeId = ExchangeId.BINANCE
    market_type: MarketType = MarketType.FUTURES_USDT
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    symbol: str
    side: PositionSide
    signal_price: Decimal = Decimal("0")
    entry_price: Decimal | None = None  # None implies Market entry at current price
    stop_loss_price: Decimal
    take_profit_targets: list[TakeProfitTargetSpec] = Field(default_factory=list)
    trailing_stop_pct: Decimal | None = None
    leverage: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)


class OrderLifecycleEvent(DomainEvent):
    """Emitted whenever an order transitions state."""

    topic: str = "order:lifecycle"
    order_id: str
    position_id: str | None = None
    client_order_id: str
    exchange_order_id: str | None = None
    symbol: str
    market_type: MarketType
    side: OrderSide
    purpose: OrderPurpose
    status: OrderStatus
    requested_qty: Decimal
    filled_qty: Decimal
    avg_fill_price: Decimal | None = None
    reason: str | None = None


class PositionUpdatedEvent(DomainEvent):
    """Emitted when a position opens, updates PnL/SL, or closes."""

    topic: str = "position:updated"
    position_id: str
    strategy_id: str | None = None
    symbol: str
    market_type: MarketType
    execution_mode: ExecutionMode
    side: PositionSide
    status: PositionStatus
    entry_price: Decimal
    current_price: Decimal = Decimal("0")  # Live mark price for UI display
    current_qty: Decimal
    stop_loss_price: Decimal | None = None
    stop_order_live: bool = False
    unrealized_pnl_usd: Decimal = Decimal("0")
    unrealized_pnl_pct: float = 0.0  # Pre-computed % for UI display
    realized_pnl_usd: Decimal = Decimal("0")
    exit_reason: str | None = None


class RiskAlertEvent(DomainEvent):
    """Emitted when a risk rule rejects a trade or triggers a circuit breaker."""

    topic: str = "risk:alert"
    severity: str = "WARNING"  # INFO | WARNING | CRITICAL
    rule_name: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


EventHandler = Callable[[DomainEvent], Awaitable[None]]


class EventBus:
    """Asynchronous pub/sub event bus with subscriber error isolation."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)
        self._wildcard_subscribers: list[EventHandler] = []
        self._published_count: int = 0
        self._error_count: int = 0

    def subscribe(self, topic: str, handler: EventHandler) -> None:
        """Register an async handler for a specific topic or '*' for all topics."""
        if topic == "*":
            if handler not in self._wildcard_subscribers:
                self._wildcard_subscribers.append(handler)
        else:
            if handler not in self._subscribers[topic]:
                self._subscribers[topic].append(handler)

    def unsubscribe(self, topic: str, handler: EventHandler) -> None:
        """Remove a previously registered handler."""
        if topic == "*":
            if handler in self._wildcard_subscribers:
                self._wildcard_subscribers.remove(handler)
        elif handler in self._subscribers.get(topic, []):
            self._subscribers[topic].remove(handler)

    async def publish(self, event: DomainEvent) -> None:
        """Dispatch an event to all matching topic and wildcard handlers concurrently.

        Exceptions in individual subscribers are caught and logged so one faulty listener
        can never crash the trading loop or block other listeners.
        """
        self._published_count += 1
        handlers = list(self._subscribers.get(event.topic, [])) + list(self._wildcard_subscribers)
        if not handlers:
            return

        results = await asyncio.gather(
            *(self._safe_invoke(handler, event) for handler in handlers),
            return_exceptions=True,
        )
        for res in results:
            if isinstance(res, Exception):
                self._error_count += 1
                logger.error("Unexpected error in EventBus gather: %s", res)

    async def _safe_invoke(self, handler: EventHandler, event: DomainEvent) -> None:
        try:
            await handler(event)
        except Exception as exc:
            self._error_count += 1
            logger.exception(
                "Subscriber %s failed while handling event %s (topic=%s): %s",
                getattr(handler, "__name__", repr(handler)),
                event.event_id,
                event.topic,
                exc,
            )

    @property
    def stats(self) -> dict[str, int]:
        """Return diagnostic bus metrics."""
        return {
            "published_count": self._published_count,
            "error_count": self._error_count,
            "topic_count": len(self._subscribers),
            "wildcard_count": len(self._wildcard_subscribers),
        }
