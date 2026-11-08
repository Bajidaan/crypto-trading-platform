"""WebSocket Stream Manager for Binance and Bybit real-time Kline and Ticker data."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import json
import logging
from typing import Any
from pydantic import BaseModel, Field

from app.core.events import EventBus, MarketCandleEvent
from app.domains.cex.adapters.base import IExchangeAdapter
from app.domains.cex.market_data.buffer import CandleBuffer
from app.domains.cex.market_data.gap_filler import CandleGapFiller
from app.domains.cex.market_data.normalizer import (
    parse_binance_kline_message,
    parse_bybit_kline_message,
)
from app.models.enums import ExchangeId, MarketType

logger = logging.getLogger(__name__)


class StreamSubscription(BaseModel):
    """Specification of an active market data subscription."""

    exchange_id: ExchangeId
    symbol: str  # Unified format "BTC/USDT"
    market_type: MarketType
    timeframe: str  # e.g. "15m"


class MarketStreamManager:
    """Manages WebSocket subscriptions, message routing, buffer updates, and gap backfilling."""

    def __init__(
        self,
        event_bus: EventBus,
        adapter_map: dict[ExchangeId, IExchangeAdapter] | None = None,
        buffer_max_size: int = 500,
    ) -> None:
        self.event_bus = event_bus
        self.adapter_map = adapter_map or {}
        self.buffer_max_size = buffer_max_size

        # Buffer registry: (exchange_id, symbol, timeframe) -> CandleBuffer
        self._buffers: dict[tuple[ExchangeId, str, str], CandleBuffer] = {}
        self._subscriptions: set[tuple[ExchangeId, str, MarketType, str]] = set()

        # Gap fillers per exchange
        self._gap_fillers: dict[ExchangeId, CandleGapFiller] = {}
        for exch_id, adapter in self.adapter_map.items():
            self._gap_fillers[exch_id] = CandleGapFiller(adapter, event_bus=self.event_bus)

        self._is_running = False
        self._last_message_time: datetime | None = None
        self._processed_messages_count: int = 0

    def register_adapter(self, adapter: IExchangeAdapter) -> None:
        """Register or update an exchange adapter for historical gap backfilling."""
        self.adapter_map[adapter.exchange_id] = adapter
        self._gap_fillers[adapter.exchange_id] = CandleGapFiller(adapter, event_bus=self.event_bus)

    def get_or_create_buffer(
        self,
        exchange_id: ExchangeId,
        symbol: str,
        timeframe: str,
        market_type: MarketType = MarketType.FUTURES_USDT,
    ) -> CandleBuffer:
        """Get an existing CandleBuffer or create a new one."""
        key = (exchange_id, symbol, timeframe)
        if key not in self._buffers:
            self._buffers[key] = CandleBuffer(
                symbol=symbol,
                timeframe=timeframe,
                market_type=market_type,
                max_size=self.buffer_max_size,
            )
        return self._buffers[key]

    def subscribe(
        self,
        exchange_id: ExchangeId,
        symbol: str,
        market_type: MarketType = MarketType.FUTURES_USDT,
        timeframe: str = "15m",
    ) -> CandleBuffer:
        """Register a subscription for a symbol and timeframe and return its buffer."""
        self._subscriptions.add((exchange_id, symbol, market_type, timeframe))
        return self.get_or_create_buffer(exchange_id, symbol, timeframe, market_type)

    async def warmup_subscription(
        self,
        exchange_id: ExchangeId,
        symbol: str,
        market_type: MarketType = MarketType.FUTURES_USDT,
        timeframe: str = "15m",
        bars_count: int = 200,
    ) -> int:
        """Fetch historical bars to warm up the buffer before live streaming starts."""
        buffer = self.get_or_create_buffer(exchange_id, symbol, timeframe, market_type)
        filler = self._gap_fillers.get(exchange_id)
        if filler:
            return await filler.warmup(buffer, market_type=market_type, limit=bars_count)
        return 0

    async def handle_raw_message(
        self,
        exchange_id: ExchangeId,
        raw_payload: str | dict[str, Any],
        market_type: MarketType = MarketType.FUTURES_USDT,
    ) -> list[MarketCandleEvent]:
        """Ingest a raw WebSocket frame from an exchange, parse, update buffers, and publish events."""
        if isinstance(raw_payload, str):
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                return []
        else:
            payload = raw_payload

        events: list[MarketCandleEvent] = []
        if exchange_id == ExchangeId.BINANCE:
            event = parse_binance_kline_message(payload, market_type=market_type)
            if event:
                events.append(event)
        elif exchange_id == ExchangeId.BYBIT:
            events.extend(parse_bybit_kline_message(payload, market_type=market_type))

        processed: list[MarketCandleEvent] = []
        for ev in events:
            buffer = self.get_or_create_buffer(
                exchange_id=ev.exchange_id,
                symbol=ev.symbol,
                timeframe=ev.timeframe,
                market_type=ev.market_type,
            )

            # Check sequence gap if filler is configured and buffer has previous bars
            filler = self._gap_fillers.get(ev.exchange_id)
            if filler and buffer.latest is not None:
                gap = filler.detect_gap(buffer, ev.open_time)
                if gap > 0:
                    logger.warning(
                        "Detected %d missing candles for %s (%s); triggering backfill...",
                        gap,
                        ev.symbol,
                        ev.timeframe,
                    )
                    await filler.backfill_gap(buffer, market_type=ev.market_type, max_bars=gap + 10)

            # Update rolling buffer
            buffer.update_or_append(ev)

            # Publish event to EventBus
            await self.event_bus.publish(ev)
            processed.append(ev)

        self._processed_messages_count += len(processed)
        self._last_message_time = datetime.now(timezone.utc)
        return processed

    @property
    def subscriptions(self) -> list[StreamSubscription]:
        return [
            StreamSubscription(
                exchange_id=e, symbol=s, market_type=m, timeframe=t
            )
            for (e, s, m, t) in self._subscriptions
        ]

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "is_running": self._is_running,
            "active_subscriptions": len(self._subscriptions),
            "buffers_count": len(self._buffers),
            "processed_messages_count": self._processed_messages_count,
            "last_message_time": self._last_message_time.isoformat()
            if self._last_message_time
            else None,
        }
