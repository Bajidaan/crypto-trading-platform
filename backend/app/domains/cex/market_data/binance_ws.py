"""Live Binance kline WebSocket feed.

Replaces REST polling as the trigger for strategy evaluation: a strategy runs the moment
Binance reports its candle closed (the kline's `x` flag), instead of up to one poll interval
later. One combined-stream connection is kept per market type (spot / USDT-M futures) and is
re-opened with the new stream list whenever the set of subscriptions changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
import json
import logging
import random
import time
from typing import Any

from app.core.events import MarketCandleEvent
from app.domains.cex.market_data.normalizer import parse_binance_kline_message
from app.models.enums import MarketType

logger = logging.getLogger(__name__)

# (unified symbol "BTC/USDT", timeframe "15m", market type)
KlineKey = tuple[str, str, MarketType]
KlineHandler = Callable[[MarketCandleEvent], Awaitable[None]]
# Returns an async context manager yielding an async-iterable connection of text frames.
ConnectFn = Callable[[str], Any]

# USDT-M futures moved market streams to path-based routing (old root URL retired 2026-04-23):
# klines are served from /market. Spot keeps the classic combined-stream URL.
FUTURES_STREAM_BASE = "wss://fstream.binance.com/market/stream?streams="
SPOT_STREAM_BASE = "wss://stream.binance.com:9443/stream?streams="

# Binance allows up to 1024 streams per connection; stay well below it.
MAX_STREAMS_PER_CONNECTION = 200


def _default_connect(url: str) -> Any:
    from websockets.asyncio.client import connect

    # Binance pings every few minutes and drops idle clients; the library answers pings itself.
    return connect(url, ping_interval=20, ping_timeout=20, max_size=2**22)


def stream_name(symbol: str, timeframe: str) -> str:
    return f"{symbol.replace('/', '').lower()}@kline_{timeframe}"


def build_stream_url(market_type: MarketType, keys: set[KlineKey]) -> str:
    names = sorted({stream_name(sym, tf) for sym, tf, mt in keys if mt == market_type})
    base = SPOT_STREAM_BASE if market_type == MarketType.SPOT else FUTURES_STREAM_BASE
    return base + "/".join(names)


class BinanceKlineStream:
    """Keeps live kline WebSocket connections for the requested (symbol, timeframe, market) keys."""

    def __init__(
        self,
        on_kline: KlineHandler,
        connect_fn: ConnectFn | None = None,
        min_backoff_sec: float = 1.0,
        max_backoff_sec: float = 30.0,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_kline = on_kline
        self._connect_fn = connect_fn or _default_connect
        self._min_backoff = min_backoff_sec
        self._max_backoff = max_backoff_sec
        self._time_fn = time_fn

        self._keys: dict[MarketType, frozenset[KlineKey]] = {}
        self._tasks: dict[MarketType, asyncio.Task[None]] = {}
        self._last_message_at: dict[KlineKey, float] = {}
        self._connected: dict[MarketType, bool] = {}
        self._running = False
        self.messages_received = 0
        self.reconnects = 0

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def set_subscriptions(self, keys: set[KlineKey]) -> None:
        """Declare the full set of klines wanted. Connections whose stream set changed restart."""
        if not self._running:
            return
        for market_type in (MarketType.SPOT, MarketType.FUTURES_USDT):
            wanted = frozenset(k for k in keys if k[2] == market_type)
            if len(wanted) > MAX_STREAMS_PER_CONNECTION:
                logger.warning(
                    "%d %s kline streams requested; only the first %d are streamed, the rest fall back to polling.",
                    len(wanted), market_type.value, MAX_STREAMS_PER_CONNECTION,
                )
                wanted = frozenset(sorted(wanted, key=lambda k: (k[0], k[1]))[:MAX_STREAMS_PER_CONNECTION])
            if wanted == self._keys.get(market_type, frozenset()) and (
                not wanted or market_type in self._tasks
            ):
                continue
            old = self._tasks.pop(market_type, None)
            if old is not None:
                old.cancel()
            self._keys[market_type] = wanted
            self._connected[market_type] = False
            if wanted:
                self._tasks[market_type] = asyncio.create_task(
                    self._connection_loop(market_type, wanted),
                    name=f"binance-kline-{market_type.value}",
                )

    # ------------------------------------------------------------------ state

    def is_fresh(self, key: KlineKey, max_age_sec: float = 10.0) -> bool:
        """True when this kline is streaming: connected and a frame arrived recently.

        Futures klines push every ~250 ms and spot every ~1-2 s while the market trades,
        so a key silent for `max_age_sec` is treated as stale and polled over REST instead.
        """
        if not self._connected.get(key[2], False):
            return False
        last = self._last_message_at.get(key)
        return last is not None and (self._time_fn() - last) <= max_age_sec

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "connected": {mt.value: ok for mt, ok in self._connected.items()},
            "streams": {mt.value: len(keys) for mt, keys in self._keys.items()},
            "messages_received": self.messages_received,
            "reconnects": self.reconnects,
        }

    # ------------------------------------------------------------------ connection

    async def _connection_loop(self, market_type: MarketType, keys: frozenset[KlineKey]) -> None:
        url = build_stream_url(market_type, set(keys))
        backoff = self._min_backoff
        while self._running:
            try:
                async with self._connect_fn(url) as ws:
                    self._connected[market_type] = True
                    backoff = self._min_backoff
                    logger.info("Binance %s kline stream connected (%d streams).", market_type.value, len(keys))
                    async for raw in ws:
                        await self._handle_frame(raw, market_type)
                # Server closed the socket cleanly (Binance does this every 24 h).
                logger.info("Binance %s kline stream closed by server; reconnecting.", market_type.value)
            except asyncio.CancelledError:
                self._connected[market_type] = False
                raise
            except Exception as exc:
                logger.warning(
                    "Binance %s kline stream error: %s; reconnecting in %.1fs", market_type.value, exc, backoff
                )
            self._connected[market_type] = False
            if not self._running:
                break
            self.reconnects += 1
            await asyncio.sleep(backoff + random.uniform(0, backoff / 4))
            backoff = min(backoff * 2, self._max_backoff)

    async def _handle_frame(self, raw: Any, market_type: MarketType) -> None:
        try:
            payload = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict):
            return
        event = parse_binance_kline_message(payload, market_type=market_type)
        if event is None:
            return
        self.messages_received += 1
        self._last_message_at[(event.symbol, event.timeframe, market_type)] = self._time_fn()
        try:
            await self._on_kline(event)
        except Exception:
            logger.exception("Kline handler failed for %s %s", event.symbol, event.timeframe)
