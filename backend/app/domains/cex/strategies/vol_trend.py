"""Built-In Strategy 4: Volatility-Scaled Trend Following with a Regime Filter and No-Trade Band.

Rules (evaluated on closed bars only; intended for daily or multi-hour bars):
  * Trend:   fast SMA vs slow SMA of the traded symbol.
  * Regime:  a benchmark (default BTC/USDT) vs its long SMA. Longs are only opened while the
             benchmark is above it, and are closed when it falls below ("rotate to cash").
  * Band:    both comparisons use a no-trade band of `band_pct` percent around the slow
             average. Inside the band nothing changes, so small wiggles around the average do
             not cause fee-paying flips.
  * Size:    position notional = equity * min(max_weight, target volatility / realized
             volatility). Calmer markets get a larger position, wilder markets a smaller one.
  * Stop:    a wide ATR stop acts only as crash protection; the normal exit is the trend rule.

The position is sized once at entry and is not re-sized while it is held.
"""

from decimal import Decimal
from typing import Any

from app.core.decimal_utils import to_decimal
from app.core.events import MarketCandleEvent, TradeSignalEvent
from app.domains.cex.market_data.buffer import CandleBuffer, timeframe_to_seconds
from app.domains.cex.strategies.base import IStrategy
from app.domains.cex.strategies.indicators import calculate_atr
from app.models.enums import MarketType, PositionSide

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
SECONDS_PER_YEAR = 365 * 86400

REGIME_ON = "RISK_ON"
REGIME_OFF = "RISK_OFF"
REGIME_NEUTRAL = "NEUTRAL"
REGIME_UNKNOWN = "UNKNOWN"


def _sma(values: list[Decimal], period: int) -> Decimal:
    return sum(values[-period:], ZERO) / Decimal(str(period))


class VolScaledTrendStrategy(IStrategy):
    """Dual moving-average trend following, gated by a benchmark regime and sized by volatility."""

    def __init__(
        self,
        strategy_id: str,
        name: str = "Volatility-Scaled Trend",
        market_type: MarketType = MarketType.FUTURES_USDT,
        symbols: list[str] | None = None,
        timeframe: str = "1d",
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
        self.fast_ma_period: int = int(p.get("fast_ma_period", 10))
        self.slow_ma_period: int = int(p.get("slow_ma_period", 30))
        self.regime_ma_period: int = int(p.get("regime_ma_period", 200))
        self.use_regime_filter: bool = bool(int(p.get("use_regime_filter", 1)))
        self.regime_symbol: str = str(p.get("regime_symbol", "BTC/USDT"))
        self.band: Decimal = to_decimal(p.get("band_pct", "1.0")) / HUNDRED
        self.vol_lookback: int = int(p.get("vol_lookback", 30))
        self.target_annual_vol: Decimal = to_decimal(p.get("target_annual_vol_pct", "25")) / HUNDRED
        self.max_weight: Decimal = to_decimal(p.get("max_weight_pct", "100")) / HUNDRED
        self.atr_period: int = int(p.get("atr_period", 14))
        self.atr_stop_multiplier: Decimal = to_decimal(p.get("atr_stop_multiplier", "4.0"))
        self.allow_short: bool = bool(int(p.get("allow_short", 0)))

        if self.fast_ma_period >= self.slow_ma_period:
            raise ValueError("fast_ma_period must be smaller than slow_ma_period.")

    @property
    def context_symbols(self) -> list[str]:
        return [self.regime_symbol] if self.use_regime_filter else []

    @property
    def required_warmup_bars(self) -> int:
        longest = max(self.slow_ma_period, self.vol_lookback + 1, self.atr_period + 1)
        if self.use_regime_filter:
            longest = max(longest, self.regime_ma_period)
        return longest + 1

    # ------------------------------------------------------------------
    # Building blocks
    # ------------------------------------------------------------------

    def _regime(self, as_of: Any) -> str:
        """Benchmark state as of the evaluated bar, with the no-trade band applied."""
        if not self.use_regime_filter:
            return REGIME_ON
        bench = self.context_buffers.get(self.regime_symbol)
        if bench is None:
            return REGIME_UNKNOWN
        closes = [c.close for c in bench.candles if c.is_closed and c.open_time <= as_of]
        if len(closes) < self.regime_ma_period:
            return REGIME_UNKNOWN
        average = _sma(closes, self.regime_ma_period)
        if closes[-1] > average * (ONE + self.band):
            return REGIME_ON
        if closes[-1] < average * (ONE - self.band):
            return REGIME_OFF
        return REGIME_NEUTRAL

    def _annualized_volatility(self, closes: list[Decimal]) -> Decimal:
        window = closes[-(self.vol_lookback + 1) :]
        returns = [window[i] / window[i - 1] - ONE for i in range(1, len(window)) if window[i - 1] > ZERO]
        if len(returns) < 2:
            return ZERO
        mean = sum(returns, ZERO) / Decimal(str(len(returns)))
        variance = sum(((r - mean) ** 2 for r in returns), ZERO) / Decimal(str(len(returns) - 1))
        bars_per_year = Decimal(str(SECONDS_PER_YEAR)) / Decimal(str(timeframe_to_seconds(self.timeframe)))
        return (variance * bars_per_year).sqrt()

    def _target_weight(self, annual_vol: Decimal) -> Decimal:
        if annual_vol <= ZERO:
            return self.max_weight
        return min(self.max_weight, self.target_annual_vol / annual_vol)

    # ------------------------------------------------------------------
    # Signal logic
    # ------------------------------------------------------------------

    def on_candle(
        self,
        candle: MarketCandleEvent,
        buffer: CandleBuffer,
    ) -> list[TradeSignalEvent]:
        if not candle.is_closed:
            return []

        bars = [c for c in buffer.candles if c.is_closed and c.open_time <= candle.open_time]
        own_need = max(self.slow_ma_period, self.vol_lookback + 1, self.atr_period + 1) + 1
        if len(bars) < own_need:
            return []

        closes = [c.close for c in bars]
        price = closes[-1]
        fast = _sma(closes, self.fast_ma_period)
        slow = _sma(closes, self.slow_ma_period)
        trend_up = fast > slow * (ONE + self.band)
        trend_down = fast < slow * (ONE - self.band)
        regime = self._regime(candle.open_time)
        # Without the filter the regime never blocks an entry and never forces an exit.
        longs_allowed = not self.use_regime_filter or regime == REGIME_ON
        shorts_allowed = not self.use_regime_filter or regime == REGIME_OFF
        regime_against_long = self.use_regime_filter and regime == REGIME_OFF
        regime_against_short = self.use_regime_filter and regime == REGIME_ON

        symbol = candle.symbol
        held = self.current_side(symbol)
        metadata = {
            "fast_ma": float(fast),
            "slow_ma": float(slow),
            "regime": regime,
        }

        # --- Manage an open position: exit when the trend or the regime turns against it.
        if held == PositionSide.LONG:
            if trend_down or regime_against_long:
                reason = "TREND_DOWN" if trend_down else "REGIME_OFF"
                return [self.create_exit_signal(symbol, held, price, {**metadata, "exit_trigger": reason})]
            return []
        if held == PositionSide.SHORT:
            if trend_up or regime_against_short:
                reason = "TREND_UP" if trend_up else "REGIME_ON"
                return [self.create_exit_signal(symbol, held, price, {**metadata, "exit_trigger": reason})]
            return []

        # --- Flat: look for a new entry.
        side: PositionSide | None = None
        if trend_up and longs_allowed:
            side = PositionSide.LONG
        elif (
            self.allow_short
            and self.market_type == MarketType.FUTURES_USDT
            and trend_down
            and shorts_allowed
        ):
            side = PositionSide.SHORT
        if side is None:
            return []

        atrs = calculate_atr([c.high for c in bars], [c.low for c in bars], closes, self.atr_period)
        if not atrs:
            return []
        stop_distance = atrs[-1] * self.atr_stop_multiplier
        if side == PositionSide.LONG:
            stop_loss = max(price - stop_distance, price * Decimal("0.5"))
        else:
            stop_loss = price + stop_distance

        annual_vol = self._annualized_volatility(closes)
        weight = self._target_weight(annual_vol)
        if weight <= ZERO:
            return []

        return [
            self.create_signal(
                symbol=symbol,
                side=side,
                signal_price=price,
                stop_loss_price=stop_loss,
                target_weight=weight,
                metadata={
                    **metadata,
                    "annual_volatility_pct": float(annual_vol * HUNDRED),
                    "target_weight_pct": float(weight * HUNDRED),
                    "atr": float(atrs[-1]),
                },
            )
        ]
