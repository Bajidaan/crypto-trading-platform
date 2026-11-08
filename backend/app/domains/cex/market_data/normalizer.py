"""Parses and normalizes raw WebSocket messages from Binance and Bybit into MarketCandleEvent."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.core.decimal_utils import to_decimal
from app.core.events import MarketCandleEvent
from app.models.enums import ExchangeId, MarketType

BYBIT_REVERSE_INTERVAL_MAP = {
    "1": "1m",
    "3": "3m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "120": "2h",
    "240": "4h",
    "360": "6h",
    "720": "12h",
    "D": "1d",
    "d": "1d",
    "W": "1w",
    "w": "1w",
}


def normalize_symbol_to_unified(raw_symbol: str) -> str:
    """Convert exchange symbol e.g. 'BTCUSDT' to unified 'BTC/USDT'."""
    s = raw_symbol.upper().strip()
    if "/" in s:
        return s
    if s.endswith("USDT"):
        return f"{s[:-4]}/USDT"
    if s.endswith("USDC"):
        return f"{s[:-4]}/USDC"
    if s.endswith("BTC"):
        return f"{s[:-3]}/BTC"
    return s


def parse_binance_kline_message(
    payload: dict[str, Any],
    market_type: MarketType = MarketType.FUTURES_USDT,
) -> MarketCandleEvent | None:
    """Parse Binance Kline WS event (single or combined stream) into MarketCandleEvent."""
    # Unwrap combined stream if wrapped
    data = payload.get("data", payload)
    if data.get("e") != "kline":
        return None

    k = data.get("k", {})
    if not k:
        return None

    raw_symbol = str(k.get("s") or data.get("s", ""))
    open_time_ms = int(k.get("t", 0))
    timeframe = str(k.get("i", "1m"))
    open_time = datetime.fromtimestamp(open_time_ms / 1000.0, tz=timezone.utc)

    return MarketCandleEvent(
        exchange_id=ExchangeId.BINANCE,
        symbol=normalize_symbol_to_unified(raw_symbol),
        market_type=market_type,
        timeframe=timeframe,
        open_time=open_time,
        open=to_decimal(k.get("o", "0")),
        high=to_decimal(k.get("h", "0")),
        low=to_decimal(k.get("l", "0")),
        close=to_decimal(k.get("c", "0")),
        volume=to_decimal(k.get("v", "0")),
        is_closed=bool(k.get("x", False)),
    )


def parse_bybit_kline_message(
    payload: dict[str, Any],
    market_type: MarketType = MarketType.FUTURES_USDT,
) -> list[MarketCandleEvent]:
    """Parse Bybit v5 Kline WS event into MarketCandleEvent list."""
    topic = str(payload.get("topic", ""))
    if not topic.startswith("kline"):
        return []

    # Topic format: "kline.{interval}.{symbol}" e.g. "kline.15.BTCUSDT"
    parts = topic.split(".")
    topic_interval = parts[1] if len(parts) >= 3 else "15"
    topic_symbol = parts[2] if len(parts) >= 3 else ""

    data_items = payload.get("data", [])
    if isinstance(data_items, dict):
        data_items = [data_items]

    events: list[MarketCandleEvent] = []
    for item in data_items:
        raw_interval = str(item.get("interval") or topic_interval)
        timeframe = BYBIT_REVERSE_INTERVAL_MAP.get(raw_interval, raw_interval)
        raw_symbol = str(topic_symbol or "")
        open_time_ms = int(item.get("start", 0))
        open_time = datetime.fromtimestamp(open_time_ms / 1000.0, tz=timezone.utc)

        events.append(
            MarketCandleEvent(
                exchange_id=ExchangeId.BYBIT,
                symbol=normalize_symbol_to_unified(raw_symbol),
                market_type=market_type,
                timeframe=timeframe,
                open_time=open_time,
                open=to_decimal(item.get("open", "0")),
                high=to_decimal(item.get("high", "0")),
                low=to_decimal(item.get("low", "0")),
                close=to_decimal(item.get("close", "0")),
                volume=to_decimal(item.get("volume", item.get("turnover", "0"))),
                is_closed=bool(item.get("confirm", False)),
            )
        )

    return events
