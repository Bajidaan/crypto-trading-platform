"""Built-In Strategy 1: Trend-Following EMA Crossover + RSI Filter + ATR Stop-Loss."""

from decimal import Decimal
from typing import Any

from app.core.decimal_utils import to_decimal
from app.core.events import MarketCandleEvent, TakeProfitTargetSpec, TradeSignalEvent
from app.domains.cex.market_data.buffer import CandleBuffer
from app.domains.cex.strategies.base import IStrategy
from app.domains.cex.strategies.indicators import (
    calculate_atr,
    calculate_ema,
    calculate_rsi,
)
from app.models.enums import MarketType, PositionSide


class EmaRsiAtrTrendStrategy(IStrategy):
    """Trend-following strategy utilizing EMA crossover, RSI momentum filter, and ATR-based risk."""

    def __init__(
        self,
        strategy_id: str,
        name: str = "EMA RSI ATR Trend",
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
        self.fast_ema_period: int = int(p.get("fast_ema_period", 9))
        self.slow_ema_period: int = int(p.get("slow_ema_period", 21))
        self.rsi_period: int = int(p.get("rsi_period", 14))
        self.rsi_long_threshold: Decimal = to_decimal(p.get("rsi_long_threshold", "50.0"))
        self.rsi_short_threshold: Decimal = to_decimal(p.get("rsi_short_threshold", "50.0"))
        self.atr_period: int = int(p.get("atr_period", 14))
        self.atr_sl_multiplier: Decimal = to_decimal(p.get("atr_sl_multiplier", "1.5"))
        self.tp1_r_multiplier: Decimal = to_decimal(p.get("tp1_r_multiplier", "1.5"))
        self.tp2_r_multiplier: Decimal = to_decimal(p.get("tp2_r_multiplier", "3.0"))
        self.only_on_candle_close: bool = bool(p.get("only_on_candle_close", True))

        self._last_signal_bar_time: Any = None

    @property
    def required_warmup_bars(self) -> int:
        return max(self.slow_ema_period, self.rsi_period, self.atr_period) + 10

    def on_candle(
        self,
        candle: MarketCandleEvent,
        buffer: CandleBuffer,
    ) -> list[TradeSignalEvent]:
        # Only evaluate on candle close if configured
        if self.only_on_candle_close and not candle.is_closed:
            return []

        # Prevent duplicate signal on the same candle bar
        if self._last_signal_bar_time == candle.open_time:
            return []

        if not buffer.is_warmed_up(self.required_warmup_bars):
            return []

        closes = buffer.get_closes()
        highs = buffer.get_highs()
        lows = buffer.get_lows()

        fast_emas = calculate_ema(closes, self.fast_ema_period)
        slow_emas = calculate_ema(closes, self.slow_ema_period)
        rsis = calculate_rsi(closes, self.rsi_period)
        atrs = calculate_atr(highs, lows, closes, self.atr_period)

        if len(fast_emas) < 2 or len(slow_emas) < 2 or not rsis or not atrs:
            return []

        # Current and previous values of EMAs
        fast_curr = fast_emas[-1]
        fast_prev = fast_emas[-2]
        slow_curr = slow_emas[-1]
        slow_prev = slow_emas[-2]

        current_rsi = rsis[-1]
        current_atr = atrs[-1]
        current_price = candle.close

        signals: list[TradeSignalEvent] = []

        # Check Bullish Crossover: Fast EMA crosses ABOVE Slow EMA + RSI filter
        bullish_crossover = (fast_prev <= slow_prev) and (fast_curr > slow_curr)
        if bullish_crossover and current_rsi >= self.rsi_long_threshold:
            sl_distance = current_atr * self.atr_sl_multiplier
            stop_loss = current_price - sl_distance
            risk_r = sl_distance

            tp1 = current_price + (risk_r * self.tp1_r_multiplier)
            tp2 = current_price + (risk_r * self.tp2_r_multiplier)

            tp_targets = [
                TakeProfitTargetSpec(
                    target_index=1,
                    price=tp1,
                    close_Dynamic_pct=Decimal("50"),
                    move_sl_to_breakeven=True,
                ),
                TakeProfitTargetSpec(
                    target_index=2,
                    price=tp2,
                    close_Dynamic_pct=Decimal("50"),
                    move_sl_to_breakeven=False,
                ),
            ]

            sig = self.create_signal(
                symbol=candle.symbol,
                side=PositionSide.LONG,
                signal_price=current_price,
                stop_loss_price=stop_loss,
                take_profit_targets=tp_targets,
                metadata={
                    "fast_ema": float(fast_curr),
                    "slow_ema": float(slow_curr),
                    "rsi": float(current_rsi),
                    "atr": float(current_atr),
                },
            )
            signals.append(sig)
            self._last_signal_bar_time = candle.open_time
            return signals

        # Check Bearish Crossover (Futures only): Fast EMA crosses BELOW Slow EMA + RSI filter
        bearish_crossover = (fast_prev >= slow_prev) and (fast_curr < slow_curr)
        if (
            bearish_crossover
            and self.market_type == MarketType.FUTURES_USDT
            and current_rsi <= self.rsi_short_threshold
        ):
            sl_distance = current_atr * self.atr_sl_multiplier
            stop_loss = current_price + sl_distance
            risk_r = sl_distance

            tp1 = current_price - (risk_r * self.tp1_r_multiplier)
            tp2 = current_price - (risk_r * self.tp2_r_multiplier)

            tp_targets = [
                TakeProfitTargetSpec(
                    target_index=1,
                    price=tp1,
                    close_Dynamic_pct=Decimal("50"),
                    move_sl_to_breakeven=True,
                ),
                TakeProfitTargetSpec(
                    target_index=2,
                    price=tp2,
                    close_Dynamic_pct=Decimal("50"),
                    move_sl_to_breakeven=False,
                ),
            ]

            sig = self.create_signal(
                symbol=candle.symbol,
                side=PositionSide.SHORT,
                signal_price=current_price,
                stop_loss_price=stop_loss,
                take_profit_targets=tp_targets,
                metadata={
                    "fast_ema": float(fast_curr),
                    "slow_ema": float(slow_curr),
                    "rsi": float(current_rsi),
                    "atr": float(current_atr),
                },
            )
            signals.append(sig)
            self._last_signal_bar_time = candle.open_time

        return signals
