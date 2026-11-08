"""Abstract Base Strategy Interface and Canonical Signal Generation."""

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

from app.core.events import (
    MarketCandleEvent,
    OrderLifecycleEvent,
    PositionUpdatedEvent,
    TakeProfitTargetSpec,
    TradeSignalEvent,
)
from app.domains.cex.adapters.base import TickerSnapshot
from app.domains.cex.market_data.buffer import CandleBuffer
from app.models.enums import MarketType, PositionSide, SignalSource


class IStrategy(ABC):
    """Unified interface for all algorithmic trading strategies (Built-in & Custom plugins)."""

    def __init__(
        self,
        strategy_id: str,
        name: str,
        market_type: MarketType = MarketType.FUTURES_USDT,
        symbols: list[str] | None = None,
        timeframe: str = "15m",
        parameters: dict[str, Any] | None = None,
        risk_config: dict[str, Any] | None = None,
    ) -> None:
        self.strategy_id = strategy_id
        self.name = name
        self.market_type = market_type
        self.symbols = symbols or ["BTC/USDT"]
        self.timeframe = timeframe
        self.parameters = parameters or {}
        self.risk_config = risk_config or {}

    @property
    @abstractmethod
    def required_warmup_bars(self) -> int:
        """Minimum number of historical candle bars needed before strategy begins evaluating signals."""

    @abstractmethod
    def on_candle(
        self,
        candle: MarketCandleEvent,
        buffer: CandleBuffer,
    ) -> list[TradeSignalEvent]:
        """Evaluate trading logic upon arrival of a candle update or candle close."""

    def on_ticker(self, ticker: TickerSnapshot) -> list[TradeSignalEvent]:
        """Optional hook for tick-level evaluation (default: no-op)."""
        return []

    def on_order_event(self, event: OrderLifecycleEvent) -> None:
        """Optional hook to react to order execution fills, cancellations, or rejections."""
        return None

    def on_position_event(self, event: PositionUpdatedEvent) -> None:
        """Optional hook to track position status, realized PnL, or emergency closures."""
        return None

    def create_signal(
        self,
        symbol: str,
        side: PositionSide,
        signal_price: Decimal,
        stop_loss_price: Decimal,
        take_profit_targets: list[TakeProfitTargetSpec] | None = None,
        metadata: dict[str, Any] | None = None,
        source: SignalSource = SignalSource.STRATEGY,
    ) -> TradeSignalEvent:
        """Construct a standardized TradeSignalEvent bound to this strategy."""
        return TradeSignalEvent(
            strategy_id=self.strategy_id,
            source=source,
            symbol=symbol,
            market_type=self.market_type,
            side=side,
            signal_price=signal_price,
            stop_loss_price=stop_loss_price,
            take_profit_targets=take_profit_targets or [],
            metadata={
                "strategy_name": self.name,
                "timeframe": self.timeframe,
                **(metadata or {}),
            },
        )
