"""In-memory rolling CandleBuffer for real-time bar management and indicator feeds."""

from collections import deque
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.decimal_utils import to_decimal
from app.core.events import MarketCandleEvent
from app.domains.cex.adapters.base import CandleBar
from app.models.enums import MarketType

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
}


def timeframe_to_seconds(timeframe: str) -> int:
    """Convert timeframe string e.g. '15m' to seconds duration."""
    tf = timeframe.strip().lower()
    if tf in TIMEFRAME_SECONDS:
        return TIMEFRAME_SECONDS[tf]
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    if tf.endswith("h"):
        return int(tf[:-1]) * 3600
    if tf.endswith("d"):
        return int(tf[:-1]) * 86400
    if tf.endswith("w"):
        return int(tf[:-1]) * 604800
    return 60


class CandleBuffer:
    """Thread/coroutine-safe rolling buffer of OHLCV candles for a single (symbol, timeframe)."""

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        market_type: MarketType = MarketType.FUTURES_USDT,
        max_size: int = 500,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.market_type = market_type
        self.max_size = max_size
        self._candles: deque[CandleBar] = deque(maxlen=max_size)

    def _to_candle_bar(self, item: CandleBar | MarketCandleEvent) -> CandleBar:
        if isinstance(item, CandleBar):
            return item
        return CandleBar(
            symbol=item.symbol,
            market_type=item.market_type,
            timeframe=item.timeframe,
            open_time=item.open_time,
            open=item.open,
            high=item.high,
            low=item.low,
            close=item.close,
            volume=item.volume,
            is_closed=item.is_closed,
        )

    def update_or_append(self, candle: CandleBar | MarketCandleEvent) -> bool:
        """Update the active in-progress candle or append a new candle.

        Returns:
            True if a NEW candle bar was appended (new bar opened).
            False if an existing in-progress candle was updated.
        """
        bar = self._to_candle_bar(candle)
        if not self._candles:
            self._candles.append(bar)
            return True

        latest = self._candles[-1]
        if bar.open_time == latest.open_time:
            # Update existing bar in-place
            self._candles[-1] = bar
            return False
        elif bar.open_time > latest.open_time:
            # New bar opened
            self._candles.append(bar)
            return True
        else:
            # Out-of-order historical bar: insert in sorted position if within window
            self._insert_sorted(bar)
            return False

    def _insert_sorted(self, bar: CandleBar) -> None:
        """Insert a historical bar preserving ascending open_time order."""
        bars = list(self._candles)
        # Check if already present
        for i, existing in enumerate(bars):
            if existing.open_time == bar.open_time:
                bars[i] = bar
                self._candles = deque(bars, maxlen=self.max_size)
                return
        bars.append(bar)
        bars.sort(key=lambda c: c.open_time)
        self._candles = deque(bars[-self.max_size :], maxlen=self.max_size)

    def load_history(self, candles: list[CandleBar]) -> int:
        """Bulk load historical candles, deduplicating and maintaining ascending order."""
        if not candles:
            return 0
        existing_map = {c.open_time: c for c in self._candles}
        for c in candles:
            existing_map[c.open_time] = c

        sorted_bars = sorted(existing_map.values(), key=lambda c: c.open_time)
        self._candles = deque(sorted_bars[-self.max_size :], maxlen=self.max_size)
        return len(candles)

    @property
    def latest(self) -> CandleBar | None:
        """Return the most recent candle bar (in-progress or closed)."""
        return self._candles[-1] if self._candles else None

    @property
    def latest_closed(self) -> CandleBar | None:
        """Return the most recent confirmed closed candle bar."""
        if not self._candles:
            return None
        # Walk backward from end
        for c in reversed(self._candles):
            if c.is_closed:
                return c
        return None

    @property
    def count(self) -> int:
        """Number of candles currently held in the buffer."""
        return len(self._candles)

    def is_warmed_up(self, min_bars: int) -> bool:
        """Check if buffer has accumulated enough bars for indicator calculations."""
        return len(self._candles) >= min_bars

    def get_open_times(self) -> list[datetime]:
        return [c.open_time for c in self._candles]

    def get_opens(self) -> list[Decimal]:
        return [c.open for c in self._candles]

    def get_highs(self) -> list[Decimal]:
        return [c.high for c in self._candles]

    def get_lows(self) -> list[Decimal]:
        return [c.low for c in self._candles]

    def get_closes(self) -> list[Decimal]:
        return [c.close for c in self._candles]

    def get_volumes(self) -> list[Decimal]:
        return [c.volume for c in self._candles]

    @property
    def candles(self) -> list[CandleBar]:
        return list(self._candles)

    def to_dict_list(self) -> list[dict[str, Any]]:
        """Export candle list as serialized dicts (e.g. for charts/APIs)."""
        return [
            {
                "time": int(c.open_time.timestamp()),
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
                "volume": float(c.volume),
                "is_closed": c.is_closed,
            }
            for c in self._candles
        ]
