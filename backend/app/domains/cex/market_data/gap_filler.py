"""Historical OHLCV Warmup and Sequence Gap Backfiller."""

from datetime import datetime, timezone
import logging
from typing import Any

from app.core.events import EventBus, MarketCandleEvent
from app.domains.cex.adapters.base import CandleBar, IExchangeAdapter
from app.domains.cex.market_data.buffer import CandleBuffer, timeframe_to_seconds
from app.models.enums import MarketType

logger = logging.getLogger(__name__)


class CandleGapFiller:
    """Detects sequence gaps in live streaming candles and fetches missing historical bars."""

    def __init__(
        self,
        adapter: IExchangeAdapter,
        event_bus: EventBus | None = None,
    ) -> None:
        self.adapter = adapter
        self.event_bus = event_bus

    async def warmup(
        self,
        buffer: CandleBuffer,
        market_type: MarketType = MarketType.FUTURES_USDT,
        limit: int = 200,
    ) -> int:
        """Fetch historical bars to initialize the candle buffer before live trading begins."""
        try:
            bars = await self.adapter.fetch_ohlcv(
                symbol=buffer.symbol,
                market_type=market_type,
                timeframe=buffer.timeframe,
                limit=limit,
            )
            loaded = buffer.load_history(bars)
            logger.info(
                "Warmed up %s (%s, %s) with %d historical candles",
                buffer.symbol,
                buffer.timeframe,
                market_type.value,
                loaded,
            )
            return loaded
        except Exception as exc:
            logger.error(
                "Warmup failed for %s (%s, %s): %s",
                buffer.symbol,
                buffer.timeframe,
                market_type.value,
                exc,
            )
            return 0

    def detect_gap(
        self,
        buffer: CandleBuffer,
        incoming_open_time: datetime,
    ) -> int:
        """Calculate the number of missing bars between the buffer's latest bar and incoming bar.

        Returns:
            0 if incoming bar is the immediate next bar or an update to current bar.
            > 0 if one or more bars are missing in the sequence.
        """
        if buffer.latest is None:
            return 0

        bar_seconds = timeframe_to_seconds(buffer.timeframe)
        time_diff = (incoming_open_time - buffer.latest.open_time).total_seconds()

        # If incoming bar is same or previous, no missing gap
        if time_diff <= bar_seconds:
            return 0

        # E.g. if time_diff is 2 * bar_seconds, 1 bar was skipped
        missing_bars = int(round(time_diff / bar_seconds)) - 1
        return max(0, missing_bars)

    async def backfill_gap(
        self,
        buffer: CandleBuffer,
        market_type: MarketType = MarketType.FUTURES_USDT,
        max_bars: int = 100,
    ) -> list[CandleBar]:
        """Fetch missing candles from the exchange and load them into the buffer."""
        if buffer.latest is None:
            return []

        since_ms = int(buffer.latest.open_time.timestamp() * 1000)
        try:
            bars = await self.adapter.fetch_ohlcv(
                symbol=buffer.symbol,
                market_type=market_type,
                timeframe=buffer.timeframe,
                since_ms=since_ms,
                limit=max_bars,
            )
            if not bars:
                return []

            buffer.load_history(bars)
            logger.info(
                "Backfilled %d missing candles for %s (%s)",
                len(bars),
                buffer.symbol,
                buffer.timeframe,
            )

            # Publish event for closed recovered bars if event_bus is present
            if self.event_bus is not None:
                for b in bars:
                    if b.is_closed:
                        event = MarketCandleEvent(
                            exchange_id=self.adapter.exchange_id,
                            symbol=b.symbol,
                            market_type=b.market_type,
                            timeframe=b.timeframe,
                            open_time=b.open_time,
                            open=b.open,
                            high=b.high,
                            low=b.low,
                            close=b.close,
                            volume=b.volume,
                            is_closed=True,
                        )
                        await self.event_bus.publish(event)

            return bars
        except Exception as exc:
            logger.error(
                "Failed to backfill candle gap for %s (%s): %s",
                buffer.symbol,
                buffer.timeframe,
                exc,
            )
            return []
