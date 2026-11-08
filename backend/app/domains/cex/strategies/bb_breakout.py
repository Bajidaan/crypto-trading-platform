"""Built-In Strategy 2: Bollinger Bands Volatility Breakout + ADX Trend Strength Filter."""

from decimal import Decimal
from typing import Any

from app.core.decimal_utils import to_decimal
from app.core.events import MarketCandleEvent, TakeProfitTargetSpec, TradeSignalEvent
from app.domains.cex.market_data.buffer import CandleBuffer
from app.domains.cex.strategies.base import IStrategy
from app.domains.cex.strategies.indicators import (
    calculate_adx,
    calculate_atr,
    calculate_bollinger_bands,
)
from app.models.enums import MarketType, PositionSide


class BollingerAdxBreakoutStrategy(IStrategy):
    """Volatility breakout strategy triggering on band penetrations validated by ADX trend power."""

    def __init__(
        self,
        strategy_id: str,
        name: str = "Bollinger ADX Breakout",
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
        self.bb_period: int = int(p.get("bb_period", 20))
        self.bb_std: float = float(p.get("bb_std", 2.0))
        self.adx_period: int = int(p.get("adx_period", 14))
        self.adx_threshold: Decimal = to_decimal(p.get("adx_threshold", "25.0"))
        self.atr_period: int = int(p.get("atr_period", 14))
        self.atr_sl_multiplier: Decimal = to_decimal(p.get("atr_sl_multiplier", "1.5"))
        self.tp_r_multiplier: Decimal = to_decimal(p.get("tp_r_multiplier", "2.5"))
        self.only_on_candle_close: bool = bool(p.get("only_on_candle_close", True))

        self._last_signal_bar_time: Any = None

    @property
    def required_warmup_bars(self) -> int:
        return max(self.bb_period, self.adx_period * 2, self.atr_period) + 5

    def on_candle(
        self,
        candle: MarketCandleEvent,
        buffer: CandleBuffer,
    ) -> list[TradeSignalEvent]:
        if self.only_on_candle_close and not candle.is_closed:
            return []

        if self._last_signal_bar_time == candle.open_time:
            return []

        if not buffer.is_warmed_up(self.required_warmup_bars):
            return []

        closes = buffer.get_closes()
        highs = buffer.get_highs()
        lows = buffer.get_lows()

        if len(closes) < self.bb_period + 1:
            return []

        # Current and previous candle close
        prev_close = closes[-2]
        current_close = closes[-1]

        # Calculate current Bollinger Bands
        upper_bb, mid_bb, lower_bb = calculate_bollinger_bands(
            closes, period=self.bb_period, num_std=self.bb_std
        )
        # Calculate ADX & ATR
        current_adx = calculate_adx(highs, lows, closes, period=self.adx_period)
        atrs = calculate_atr(highs, lows, closes, period=self.atr_period)
        if not atrs:
            return []
        current_atr = atrs[-1]

        signals: list[TradeSignalEvent] = []

        # Filter by ADX trend strength
        if current_adx < self.adx_threshold:
            return []

        # Bullish Breakout: price breaks ABOVE Upper Band
        if prev_close <= upper_bb and current_close > upper_bb:
            # SL is the greater of (close - atr*multiplier) or mid_bb
            sl_candidate = current_close - (current_atr * self.atr_sl_multiplier)
            stop_loss = max(sl_candidate, mid_bb) if mid_bb < current_close else sl_candidate
            risk_r = current_close - stop_loss
            if risk_r <= Decimal("0"):
                risk_r = current_atr

            tp_price = current_close + (risk_r * self.tp_r_multiplier)
            tp_targets = [
                TakeProfitTargetSpec(
                    target_index=1,
                    price=tp_price,
                    close_Dynamic_pct=Decimal("100"),
                    move_sl_to_breakeven=True,
                )
            ]

            sig = self.create_signal(
                symbol=candle.symbol,
                side=PositionSide.LONG,
                signal_price=current_close,
                stop_loss_price=stop_loss,
                take_profit_targets=tp_targets,
                metadata={
                    "upper_bb": float(upper_bb),
                    "lower_bb": float(lower_bb),
                    "mid_bb": float(mid_bb),
                    "adx": float(current_adx),
                    "atr": float(current_atr),
                },
            )
            signals.append(sig)
            self._last_signal_bar_time = candle.open_time
            return signals

        # Bearish Breakdown (Futures only): price breaks BELOW Lower Band
        if (
            prev_close >= lower_bb
            and current_close < lower_bb
            and self.market_type == MarketType.FUTURES_USDT
        ):
            sl_candidate = current_close + (current_atr * self.atr_sl_multiplier)
            stop_loss = min(sl_candidate, mid_bb) if mid_bb > current_close else sl_candidate
            risk_r = stop_loss - current_close
            if risk_r <= Decimal("0"):
                risk_r = current_atr

            tp_price = current_close - (risk_r * self.tp_r_multiplier)
            tp_targets = [
                TakeProfitTargetSpec(
                    target_index=1,
                    price=tp_price,
                    close_Dynamic_pct=Decimal("100"),
                    move_sl_to_breakeven=True,
                )
            ]

            sig = self.create_signal(
                symbol=candle.symbol,
                side=PositionSide.SHORT,
                signal_price=current_close,
                stop_loss_price=stop_loss,
                take_profit_targets=tp_targets,
                metadata={
                    "upper_bb": float(upper_bb),
                    "lower_bb": float(lower_bb),
                    "mid_bb": float(mid_bb),
                    "adx": float(current_adx),
                    "atr": float(current_atr),
                },
            )
            signals.append(sig)
            self._last_signal_bar_time = candle.open_time

        return signals
