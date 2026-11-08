"""Built-In Strategy 3: Smart Dollar-Cost Averaging (DCA) Scale-In with Dynamic Profit Target."""

from decimal import Decimal
from typing import Any

from app.core.decimal_utils import to_decimal
from app.core.events import (
    MarketCandleEvent,
    OrderLifecycleEvent,
    PositionUpdatedEvent,
    TakeProfitTargetSpec,
    TradeSignalEvent,
)
from app.domains.cex.market_data.buffer import CandleBuffer
from app.domains.cex.strategies.base import IStrategy
from app.domains.cex.strategies.indicators import calculate_rsi
from app.models.enums import MarketType, OrderPurpose, OrderStatus, PositionSide, PositionStatus


class SmartDcaScaleInStrategy(IStrategy):
    """Adaptive scale-in Dollar-Cost Averaging strategy with multi-step safety orders."""

    def __init__(
        self,
        strategy_id: str,
        name: str = "Smart DCA Scale-In",
        market_type: MarketType = MarketType.FUTURES_USDT,
        symbols: list[str] | None = None,
        timeframe: str = "15m",
        parameters: dict[str, Any] | None = None,
        risk_config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            strategy_id=strategy_id,
            name=name,
            market_type=market_type,
            symbols=symbols,
            timeframe=timeframe,
            parameters=parameters,
            risk_config=risk_config,
        )
        p = self.parameters
        self.rsi_period: int = int(p.get("rsi_period", 14))
        self.base_entry_rsi_threshold: Decimal = to_decimal(p.get("base_entry_rsi_threshold", "35.0"))
        self.tp_target_pct: Decimal = to_decimal(p.get("tp_target_pct", "1.5"))  # +1.5% TP
        self.step_drop_pct: Decimal = to_decimal(p.get("step_drop_pct", "2.0"))  # -2.0% per safety step
        self.step_multiplier: Decimal = to_decimal(p.get("step_multiplier", "1.2"))
        self.max_safety_orders: int = int(p.get("max_safety_orders", 3))
        self.hard_stop_loss_pct: Decimal = to_decimal(p.get("hard_stop_loss_pct", "8.0"))  # -8% basket SL

        self._position_active: bool = False
        self._current_safety_step: int = 0
        self._base_entry_price: Decimal | None = None
        self._weighted_avg_entry: Decimal | None = None
        self._total_qty: Decimal = Decimal("0")
        self._last_signal_bar_time: Any = None

    @property
    def required_warmup_bars(self) -> int:
        return self.rsi_period + 10

    def on_candle(
        self,
        candle: MarketCandleEvent,
        buffer: CandleBuffer,
    ) -> list[TradeSignalEvent]:
        if not candle.is_closed:
            return []

        if self._last_signal_bar_time == candle.open_time:
            return []

        if not buffer.is_warmed_up(self.required_warmup_bars):
            return []

        current_price = candle.close
        signals: list[TradeSignalEvent] = []

        # 1. If NO position active: look for oversold base entry signal
        if not self._position_active:
            closes = buffer.get_closes()
            rsis = calculate_rsi(closes, self.rsi_period)
            if not rsis:
                return []
            current_rsi = rsis[-1]

            if current_rsi <= self.base_entry_rsi_threshold:
                # Trigger base entry
                sl_price = current_price * (Decimal("1") - (self.hard_stop_loss_pct / Decimal("100")))
                tp_price = current_price * (Decimal("1") + (self.tp_target_pct / Decimal("100")))

                tp_targets = [
                    TakeProfitTargetSpec(
                        target_index=1,
                        price=tp_price,
                        close_Dynamic_pct=Decimal("100"),
                        move_sl_to_breakeven=False,
                    )
                ]

                sig = self.create_signal(
                    symbol=candle.symbol,
                    side=PositionSide.LONG,
                    signal_price=current_price,
                    stop_loss_price=sl_price,
                    take_profit_targets=tp_targets,
                    metadata={
                        "dca_order_type": "BASE_ENTRY",
                        "rsi": float(current_rsi),
                        "safety_step": 0,
                    },
                )
                signals.append(sig)
                self._last_signal_bar_time = candle.open_time
                self._position_active = True
                self._base_entry_price = current_price
                self._weighted_avg_entry = current_price
                self._current_safety_step = 0
                return signals

        # 2. If position IS active: check if price dropped enough to trigger DCA safety order
        elif (
            self._position_active
            and self._base_entry_price is not None
            and self._current_safety_step < self.max_safety_orders
        ):
            # Calculate required drop for next step
            step_num = self._current_safety_step + 1
            required_drop_pct = self.step_drop_pct * (self.step_multiplier ** Decimal(str(step_num - 1)))
            drop_from_base = (
                (self._base_entry_price - current_price) / self._base_entry_price
            ) * Decimal("100")

            if drop_from_base >= required_drop_pct:
                # Trigger DCA Safety scale-in order
                self._current_safety_step += 1
                sl_price = self._base_entry_price * (
                    Decimal("1") - (self.hard_stop_loss_pct / Decimal("100"))
                )
                # Dynamic TP recalculated above new projected average entry
                tp_price = current_price * (Decimal("1") + (self.tp_target_pct / Decimal("100")))

                sig = self.create_signal(
                    symbol=candle.symbol,
                    side=PositionSide.LONG,
                    signal_price=current_price,
                    stop_loss_price=sl_price,
                    take_profit_targets=[
                        TakeProfitTargetSpec(
                            target_index=1,
                            price=tp_price,
                            close_Dynamic_pct=Decimal("100"),
                        )
                    ],
                    metadata={
                        "dca_order_type": "SAFETY_SCALE_IN",
                        "safety_step": self._current_safety_step,
                        "drop_from_base_pct": float(drop_from_base),
                    },
                )
                signals.append(sig)
                self._last_signal_bar_time = candle.open_time

        return signals

    def on_position_event(self, event: PositionUpdatedEvent) -> None:
        """Reset internal DCA state when position is closed."""
        if event.status == PositionStatus.CLOSED:
            self._position_active = False
            self._current_safety_step = 0
            self._base_entry_price = None
            self._weighted_avg_entry = None
            self._total_qty = Decimal("0")
