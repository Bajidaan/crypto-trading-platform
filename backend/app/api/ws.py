"""Real-time WebSocket Streaming Broadcaster for Web Dashboard."""

import asyncio
import json
import logging
from typing import Any
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.deps import get_event_bus
from app.core.events import (
    DomainEvent,
    EventBus,
    MarketCandleEvent,
    OrderLifecycleEvent,
    PositionUpdatedEvent,
    RiskAlertEvent,
)

logger = logging.getLogger(__name__)
ws_router = APIRouter(tags=["WebSocket"])


class WebSocketHub:
    """Manages active browser connections and broadcasts domain events in real time."""

    def __init__(self) -> None:
        self._active_connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._subscribed = False

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._active_connections.add(ws)
        logger.info("Dashboard WebSocket client connected (total: %d)", len(self._active_connections))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._active_connections.discard(ws)
        logger.info("Dashboard WebSocket client disconnected (remaining: %d)", len(self._active_connections))

    def setup_event_bus_listener(self, event_bus: EventBus) -> None:
        if self._subscribed:
            return

        async def _forward_event(event: DomainEvent) -> None:
            if not self._active_connections:
                return

            payload: dict[str, Any] = {
                "topic": event.topic,
                "timestamp": event.timestamp.isoformat(),
            }

            if isinstance(event, MarketCandleEvent):
                payload["type"] = "KLINE"
                payload["data"] = {
                    "symbol": event.symbol,
                    "timeframe": event.timeframe,
                    "time": int(event.open_time.timestamp()),
                    "open": float(event.open),
                    "high": float(event.high),
                    "low": float(event.low),
                    "close": float(event.close),
                    "volume": float(event.volume),
                    "is_closed": event.is_closed,
                }
            elif isinstance(event, OrderLifecycleEvent):
                payload["type"] = "ORDER"
                payload["data"] = {
                    "order_id": event.order_id,
                    "symbol": event.symbol,
                    "status": event.status.value,
                    "side": event.side.value,
                    "purpose": event.purpose.value,
                    "filled_qty": float(event.filled_qty),
                    "avg_fill_price": float(event.avg_fill_price) if event.avg_fill_price else None,
                }
            elif isinstance(event, PositionUpdatedEvent):
                payload["type"] = "POSITION"
                payload["data"] = {
                    "id": event.position_id,
                    "position_id": event.position_id,
                    "symbol": event.symbol,
                    "side": event.side.value,
                    "status": event.status.value,
                    "current_qty": float(event.current_qty),
                    "entry_price": float(event.entry_price),
                    "current_price": float(event.current_price),
                    "unrealized_pnl_usd": float(event.unrealized_pnl_usd),
                    "unrealized_pnl_pct": event.unrealized_pnl_pct,
                    "realized_pnl_usd": float(event.realized_pnl_usd),
                    "exit_reason": event.exit_reason,
                    "stop_loss_price": float(event.stop_loss_price) if event.stop_loss_price else None,
                }
            elif isinstance(event, RiskAlertEvent):
                payload["type"] = "RISK_ALERT"
                payload["data"] = {
                    "severity": event.severity,
                    "rule_name": event.rule_name,
                    "message": event.message,
                }
            else:
                payload["type"] = "EVENT"
                payload["data"] = {"topic": event.topic}

            await self.broadcast(payload)

        event_bus.subscribe("*", _forward_event)
        self._subscribed = True

    async def broadcast(self, message: dict[str, Any]) -> None:
        if not self._active_connections:
            return
        text = json.dumps(message)
        dead_connections: list[WebSocket] = []
        async with self._lock:
            for ws in list(self._active_connections):
                try:
                    await ws.send_text(text)
                except Exception:
                    dead_connections.append(ws)
            for ws in dead_connections:
                self._active_connections.discard(ws)


hub = WebSocketHub()


@ws_router.websocket("/ws/stream")
async def websocket_stream_endpoint(ws: WebSocket) -> None:
    event_bus = get_event_bus()
    hub.setup_event_bus_listener(event_bus)
    await hub.connect(ws)
    try:
        while True:
            # Keep-alive receive loop
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"type": "PONG"}))
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        await hub.disconnect(ws)
