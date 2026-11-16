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
from app.models.enums import MarketType, PositionSide, SignalAction, SignalSource


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

        # Set by the host (runner or backtester) before each on_candle call: the side of this
        # strategy's open position per symbol. A symbol with no open position is absent.
        self.position_state: dict[str, PositionSide] = {}
        # Set by the host: candle buffers for every symbol listed in `context_symbols`.
        self.context_buffers: dict[str, CandleBuffer] = {}

    @property
    def context_symbols(self) -> list[str]:
        """Extra symbols (e.g. a benchmark) whose candles the host must supply in `context_buffers`."""
        return []

    def current_side(self, symbol: str) -> PositionSide | None:
        """Side of this strategy's open position on `symbol`, or None when flat."""
        return self.position_state.get(symbol)

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
        target_weight: Decimal | None = None,
    ) -> TradeSignalEvent:
        """Construct a standardized TradeSignalEvent bound to this strategy."""
        return TradeSignalEvent(
            strategy_id=self.strategy_id,
            source=source,
            symbol=symbol,
            market_type=self.market_type,
            side=side,
            target_weight=target_weight,
            signal_price=signal_price,
            stop_loss_price=stop_loss_price,
            take_profit_targets=take_profit_targets or [],
            metadata={
                "strategy_name": self.name,
                "timeframe": self.timeframe,
                **(metadata or {}),
            },
        )

    def create_exit_signal(
        self,
        symbol: str,
        side: PositionSide,
        signal_price: Decimal,
        metadata: dict[str, Any] | None = None,
    ) -> TradeSignalEvent:
        """Construct a signal asking the host to close this strategy's open `side` position."""
        return TradeSignalEvent(
            strategy_id=self.strategy_id,
            source=SignalSource.STRATEGY,
            symbol=symbol,
            market_type=self.market_type,
            side=side,
            action=SignalAction.EXIT,
            signal_price=signal_price,
            stop_loss_price=signal_price,
            metadata={
                "strategy_name": self.name,
                "timeframe": self.timeframe,
                **(metadata or {}),
            },
        )
