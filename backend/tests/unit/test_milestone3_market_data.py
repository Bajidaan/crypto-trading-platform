"""Milestone 3 Test Suite: WebSocket Streaming, Buffer Lifecycle, Normalization, & Gap Recovery."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
import pytest

from app.core.events import DomainEvent, EventBus, MarketCandleEvent
from app.domains.cex.adapters.base import (
    AccountBalance,
    CandleBar,
    ExchangePositionSnapshot,
    IExchangeAdapter,
    MarketRules,
    NormalizedOrderRequest,
    NormalizedOrderResponse,
    TickerSnapshot,
)
from app.domains.cex.market_data import (
    CandleBuffer,
    CandleGapFiller,
    MarketStreamManager,
    normalize_symbol_to_unified,
    parse_binance_kline_message,
    parse_bybit_kline_message,
    timeframe_to_seconds,
)
from app.models.enums import (
    ExchangeId,
    MarginMode,
    MarketType,
    OrderSide,
    PositionSide,
)


# =====================================================================
# 1. Timeframe Duration & Buffer Lifecycle Tests
# =====================================================================


def test_timeframe_to_seconds() -> None:
    assert timeframe_to_seconds("1m") == 60
    assert timeframe_to_seconds("3m") == 180
    assert timeframe_to_seconds("5m") == 300
    assert timeframe_to_seconds("15m") == 900
    assert timeframe_to_seconds("1h") == 3600
    assert timeframe_to_seconds("4h") == 14400
    assert timeframe_to_seconds("1d") == 86400
    assert timeframe_to_seconds("1w") == 604800


def test_candle_buffer_update_append_and_lifecycle() -> None:
    buffer = CandleBuffer(symbol="BTC/USDT", timeframe="15m", max_size=3)
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 12, 15, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 1, 12, 45, 0, tzinfo=timezone.utc)

    # 1. First bar appended
    bar0 = CandleBar(
        symbol="BTC/USDT",
        market_type=MarketType.FUTURES_USDT,
        timeframe="15m",
        open_time=t0,
        open=Decimal("65000"),
        high=Decimal("65200"),
        low=Decimal("64900"),
        close=Decimal("65100"),
        volume=Decimal("100"),
        is_closed=False,
    )
    is_new = buffer.update_or_append(bar0)
    assert is_new is True
    assert buffer.count == 1
    assert buffer.latest is not None
    assert buffer.latest.close == Decimal("65100")
    assert buffer.latest_closed is None  # bar0 is not closed

    # 2. In-progress tick updates bar0 (same open_time)
    bar0_update = CandleBar(
        symbol="BTC/USDT",
        market_type=MarketType.FUTURES_USDT,
        timeframe="15m",
        open_time=t0,
        open=Decimal("65000"),
        high=Decimal("65300"),  # New high
        low=Decimal("64900"),
        close=Decimal("65250"),  # New close
        volume=Decimal("150"),
        is_closed=True,  # Now closed
    )
    is_new = buffer.update_or_append(bar0_update)
    assert is_new is False  # Not a new bar, just an update!
    assert buffer.count == 1
    assert buffer.latest.high == Decimal("65300")
    assert buffer.latest.close == Decimal("65250")
    assert buffer.latest_closed == buffer.latest

    # 3. Append bar1 and bar2 -> count reaches max_size=3
    bar1 = bar0.model_copy(update={"open_time": t1, "close": Decimal("65400"), "is_closed": True})
    bar2 = bar0.model_copy(update={"open_time": t2, "close": Decimal("65600"), "is_closed": True})
    assert buffer.update_or_append(bar1) is True
    assert buffer.update_or_append(bar2) is True
    assert buffer.count == 3
    assert buffer.is_warmed_up(3) is True
    assert buffer.is_warmed_up(4) is False

    # 4. Append bar3 -> oldest bar0 evicted due to max_size=3
    bar3 = bar0.model_copy(update={"open_time": t3, "close": Decimal("65800"), "is_closed": True})
    assert buffer.update_or_append(bar3) is True
    assert buffer.count == 3
    assert buffer.get_open_times() == [t1, t2, t3]
    assert buffer.get_closes() == [Decimal("65400"), Decimal("65600"), Decimal("65800")]

    # 5. Serialization for frontend Lightweight Charts
    dict_list = buffer.to_dict_list()
    assert len(dict_list) == 3
    assert dict_list[-1]["close"] == 65800.0


# =====================================================================
# 2. WebSocket Raw Payload Normalizer Tests
# =====================================================================


def test_normalizer_symbol_and_binance_kline() -> None:
    assert normalize_symbol_to_unified("BTCUSDT") == "BTC/USDT"
    assert normalize_symbol_to_unified("ETHUSDC") == "ETH/USDC"
    assert normalize_symbol_to_unified("SOL/USDT") == "SOL/USDT"

    binance_raw = {
        "e": "kline",
        "E": 1700000900000,
        "s": "BTCUSDT",
        "k": {
            "t": 1700000000000,
            "T": 1700000899999,
            "s": "BTCUSDT",
            "i": "15m",
            "o": "65100.50",
            "c": "65350.00",
            "h": "65400.00",
            "l": "65050.20",
            "v": "125.450",
            "x": True,  # Candle closed
        },
    }

    event = parse_binance_kline_message(binance_raw, market_type=MarketType.FUTURES_USDT)
    assert event is not None
    assert event.exchange_id == ExchangeId.BINANCE
    assert event.symbol == "BTC/USDT"
    assert event.timeframe == "15m"
    assert event.open == Decimal("65100.50")
    assert event.high == Decimal("65400.00")
    assert event.low == Decimal("65050.20")
    assert event.close == Decimal("65350.00")
    assert event.volume == Decimal("125.450")
    assert event.is_closed is True


def test_normalizer_bybit_kline() -> None:
    bybit_raw = {
        "topic": "kline.15.ETHUSDT",
        "type": "snapshot",
        "ts": 1700000900000,
        "data": [
            {
                "start": 1700000000000,
                "end": 1700000899999,
                "interval": "15",
                "open": "3150.20",
                "close": "3185.00",
                "high": "3190.50",
                "low": "3145.00",
                "volume": "1250.75",
                "confirm": True,  # Candle closed
            }
        ],
    }

    events = parse_bybit_kline_message(bybit_raw, market_type=MarketType.FUTURES_USDT)
    assert len(events) == 1
    ev = events[0]
    assert ev.exchange_id == ExchangeId.BYBIT
    assert ev.symbol == "ETH/USDT"
    assert ev.timeframe == "15m"
    assert ev.open == Decimal("3150.20")
    assert ev.close == Decimal("3185.00")
    assert ev.is_closed is True


# =====================================================================
# 3. Gap Detection and Sequence Backfiller Tests
# =====================================================================


class MockExchangeAdapter(IExchangeAdapter):
    """Mock adapter simulating historical OHLCV data retrieval."""

    def __init__(self, exchange_id: ExchangeId = ExchangeId.BINANCE) -> None:
        self._exchange_id = exchange_id

    @property
    def exchange_id(self) -> ExchangeId:
        return self._exchange_id

    @property
    def is_testnet(self) -> bool:
        return True

    async def fetch_ohlcv(
        self,
        symbol: str,
        market_type: MarketType,
        timeframe: str,
        since_ms: int | None = None,
        limit: int = 500,
    ) -> list[CandleBar]:
        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # Return 2 missing bars (at +15m and +30m)
        return [
            CandleBar(
                symbol=symbol,
                market_type=market_type,
                timeframe=timeframe,
                open_time=datetime(2026, 1, 1, 12, 15, 0, tzinfo=timezone.utc),
                open=Decimal("65100"),
                high=Decimal("65200"),
                low=Decimal("65050"),
                close=Decimal("65150"),
                volume=Decimal("50"),
                is_closed=True,
            ),
            CandleBar(
                symbol=symbol,
                market_type=market_type,
                timeframe=timeframe,
                open_time=datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc),
                open=Decimal("65150"),
                high=Decimal("65300"),
                low=Decimal("65100"),
                close=Decimal("65250"),
                volume=Decimal("60"),
                is_closed=True,
            ),
        ]

    # Stubs for remaining abstract methods
    async def load_markets(self, market_type: MarketType, force_refresh: bool = False) -> dict[str, MarketRules]:
        return {}

    async def get_market_rules(self, symbol: str, market_type: MarketType) -> MarketRules:
        raise NotImplementedError

    async def fetch_balance(self, market_type: MarketType) -> AccountBalance:
        raise NotImplementedError

    async def fetch_ticker(self, symbol: str, market_type: MarketType) -> TickerSnapshot:
        raise NotImplementedError

    async def configure_futures_symbol(self, symbol: str, leverage: int = 3, margin_mode: MarginMode = MarginMode.ISOLATED, one_way_mode: bool = True) -> None:
        pass

    async def place_order(self, request: NormalizedOrderRequest) -> NormalizedOrderResponse:
        raise NotImplementedError

    async def place_hard_stop_loss(self, symbol: str, market_type: MarketType, side: OrderSide, quantity: Decimal, stop_price: Decimal, client_order_id: str, limit_price: Decimal | None = None) -> NormalizedOrderResponse:
        raise NotImplementedError

    async def amend_or_replace_stop_loss(self, existing_order_id: str, existing_client_order_id: str, symbol: str, market_type: MarketType, side: OrderSide, quantity: Decimal, new_stop_price: Decimal, new_client_order_id: str) -> NormalizedOrderResponse:
        raise NotImplementedError

    async def cancel_order(self, exchange_order_id: str, symbol: str, market_type: MarketType, client_order_id: str | None = None) -> bool:
        return True

    async def cancel_all_orders(self, symbol: str, market_type: MarketType) -> int:
        return 0

    async def fetch_order(self, symbol: str, market_type: MarketType, exchange_order_id: str | None = None, client_order_id: str | None = None) -> NormalizedOrderResponse:
        raise NotImplementedError

    async def fetch_open_orders(self, symbol: str | None, market_type: MarketType) -> list[NormalizedOrderResponse]:
        return []

    async def fetch_open_positions(self, market_type: MarketType, symbol: str | None = None) -> list[ExchangePositionSnapshot]:
        return []

    async def close_position_market(self, symbol: str, market_type: MarketType, side: PositionSide, quantity: Decimal, client_order_id: str) -> NormalizedOrderResponse:
        raise NotImplementedError

    async def close(self) -> None:
        pass


async def test_gap_filler_detects_and_recovers_missing_bars() -> None:
    adapter = MockExchangeAdapter(exchange_id=ExchangeId.BINANCE)
    bus = EventBus()
    filler = CandleGapFiller(adapter=adapter, event_bus=bus)

    buffer = CandleBuffer(symbol="BTC/USDT", timeframe="15m")
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    buffer.update_or_append(
        CandleBar(
            symbol="BTC/USDT",
            market_type=MarketType.FUTURES_USDT,
            timeframe="15m",
            open_time=t0,
            open=Decimal("65000"),
            high=Decimal("65100"),
            low=Decimal("64950"),
            close=Decimal("65050"),
            volume=Decimal("40"),
            is_closed=True,
        )
    )

    # Normal next bar (+15m) -> gap = 0
    t1 = datetime(2026, 1, 1, 12, 15, 0, tzinfo=timezone.utc)
    assert filler.detect_gap(buffer, t1) == 0

    # Skipped 2 bars: incoming bar arrives at +45m -> gap = 2 missing bars
    t3 = datetime(2026, 1, 1, 12, 45, 0, tzinfo=timezone.utc)
    gap = filler.detect_gap(buffer, t3)
    assert gap == 2

    # Backfill the missing bars
    backfilled = await filler.backfill_gap(buffer, market_type=MarketType.FUTURES_USDT)
    assert len(backfilled) == 2
    # Buffer now holds t0, t1 (+15m), and t2 (+30m)
    assert buffer.count == 3
    open_times = buffer.get_open_times()
    assert open_times[1] == datetime(2026, 1, 1, 12, 15, 0, tzinfo=timezone.utc)
    assert open_times[2] == datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc)


# =====================================================================
# 4. MarketStreamManager End-to-End Routing & Event Bus Ingestion
# =====================================================================


async def test_market_stream_manager_end_to_end_routing() -> None:
    bus = EventBus()
    received_events: list[MarketCandleEvent] = []

    async def candle_listener(event: DomainEvent) -> None:
        if isinstance(event, MarketCandleEvent):
            received_events.append(event)

    bus.subscribe("market:kline", candle_listener)

    adapter = MockExchangeAdapter(exchange_id=ExchangeId.BINANCE)
    manager = MarketStreamManager(event_bus=bus, adapter_map={ExchangeId.BINANCE: adapter})

    # 1. Warm up subscription
    warmed = await manager.warmup_subscription(
        exchange_id=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        market_type=MarketType.FUTURES_USDT,
        timeframe="15m",
        bars_count=2,
    )
    assert warmed == 2

    buffer = manager.get_or_create_buffer(ExchangeId.BINANCE, "BTC/USDT", "15m")
    assert buffer.count == 2

    # 2. Ingest Binance WebSocket message
    binance_frame = {
        "e": "kline",
        "E": 1700000900000,
        "s": "BTCUSDT",
        "k": {
            "t": 1700000000000,
            "T": 1700000899999,
            "s": "BTCUSDT",
            "i": "15m",
            "o": "65100.00",
            "c": "65300.00",
            "h": "65350.00",
            "l": "65050.00",
            "v": "200.0",
            "x": False,
        },
    }
    processed = await manager.handle_raw_message(ExchangeId.BINANCE, binance_frame)
    assert len(processed) == 1
    assert len(received_events) == 1
    assert received_events[0].symbol == "BTC/USDT"
    assert received_events[0].close == Decimal("65300.00")
    assert manager.stats["processed_messages_count"] == 1
