"""Real-time Notification Dispatcher (Telegram Bot, Discord Webhook, & EventBus)."""

import logging
from typing import Any
import httpx

from app.config import Settings, get_settings
from app.core.events import DomainEvent, EventBus, OrderLifecycleEvent, PositionUpdatedEvent, RiskAlertEvent
from app.models.enums import OrderStatus, PositionStatus

logger = logging.getLogger(__name__)


class NotificationDispatcher:
    """Dispatches critical trading and risk alerts to Telegram, Discord, and internal logs."""

    def __init__(
        self,
        event_bus: EventBus,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bus = event_bus
        self._settings = settings or get_settings()
        self._external_client = http_client
        self._sent_messages: list[dict[str, Any]] = []

        # Subscribe to key financial and risk topics
        self._bus.subscribe("risk:alert", self._on_risk_alert)
        self._bus.subscribe("position:updated", self._on_position_updated)
        self._bus.subscribe("order:lifecycle", self._on_order_lifecycle)

    async def _on_risk_alert(self, event: DomainEvent) -> None:
        if not isinstance(event, RiskAlertEvent):
            return
        text = f"🚨 *[RISK {event.severity}] {event.rule_name}*\n{event.message}"
        await self.send_alert(title=f"Risk Alert: {event.rule_name}", message=text, severity=event.severity)

    async def _on_position_updated(self, event: DomainEvent) -> None:
        if not isinstance(event, PositionUpdatedEvent):
            return
        if event.status == PositionStatus.OPEN:
            text = (
                f"🟢 *Position Opened ({event.execution_mode.value})*\n"
                f"Symbol: `{event.symbol}` ({event.market_type.value})\n"
                f"Side: *{event.side.value}* | Qty: `{event.current_qty}`\n"
                f"Entry: `{event.entry_price}` | SL: `{event.stop_loss_price}`"
            )
            await self.send_alert(title="Position Opened", message=text, severity="INFO")
        elif event.status == PositionStatus.CLOSED:
            emoji = "✅" if event.realized_pnl_usd >= 0 else "🔻"
            text = (
                f"{emoji} *Position Closed ({event.execution_mode.value})*\n"
                f"Symbol: `{event.symbol}` | Side: *{event.side.value}*\n"
                f"Realized PnL: `${event.realized_pnl_usd:.2f}`\n"
                f"Reason: `{event.exit_reason or 'CLOSED'}`"
            )
            await self.send_alert(title="Position Closed", message=text, severity="INFO")

    async def _on_order_lifecycle(self, event: DomainEvent) -> None:
        if not isinstance(event, OrderLifecycleEvent):
            return
        if event.status in (OrderStatus.REJECTED, OrderStatus.FAILED_SUBMIT):
            text = (
                f"⚠️ *Order {event.status.value}*\n"
                f"Symbol: `{event.symbol}` | Purpose: `{event.purpose.value}`\n"
                f"ClientOrderId: `{event.client_order_id}`\n"
                f"Reason: `{event.reason or 'Unknown'}`"
            )
            await self.send_alert(title="Order Execution Failure", message=text, severity="WARNING")

    async def send_alert(self, title: str, message: str, severity: str = "INFO") -> bool:
        """Send an alert message to configured external channels (Telegram / Discord)."""
        record = {"title": title, "message": message, "severity": severity}
        self._sent_messages.append(record)
        logger.info("[Notification - %s] %s: %s", severity, title, message.replace("\n", " | "))

        delivered = True
        if self._settings.telegram_bot_token and self._settings.telegram_chat_id:
            ok = await self._send_telegram(message)
            delivered = delivered and ok

        if self._settings.discord_webhook_url:
            ok = await self._send_discord(message)
            delivered = delivered and ok

        return delivered

    async def _send_telegram(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self._settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self._settings.telegram_chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        try:
            if self._external_client:
                resp = await self._external_client.post(url, json=payload, timeout=10.0)
            else:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(url, json=payload, timeout=10.0)
            return resp.status_code == 200
        except Exception as exc:
            logger.error("Telegram notification delivery failed: %s", exc)
            return False

    async def _send_discord(self, text: str) -> bool:
        if not self._settings.discord_webhook_url:
            return False
        payload = {"content": text}
        try:
            if self._external_client:
                resp = await self._external_client.post(
                    self._settings.discord_webhook_url, json=payload, timeout=10.0
                )
            else:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        self._settings.discord_webhook_url, json=payload, timeout=10.0
                    )
            return resp.status_code in (200, 204)
        except Exception as exc:
            logger.error("Discord notification delivery failed: %s", exc)
            return False

    @property
    def sent_messages(self) -> list[dict[str, Any]]:
        """Return in-memory history of dispatched notifications (useful for testing & UI feed)."""
        return list(self._sent_messages)
