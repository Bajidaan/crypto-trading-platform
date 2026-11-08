"""External TradingView & Custom Webhook Signal Ingestion and Authentication."""

from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from app.core.decimal_utils import to_decimal
from app.core.events import EventBus, TakeProfitTargetSpec, TradeSignalEvent
from app.core.security import verify_webhook_signature
from app.models.enums import MarketType, PositionSide, SignalSource

logger = logging.getLogger(__name__)


class WebhookSignalPayload(BaseModel):
    """Schema for external TradingView/custom webhook payload."""

    model_config = ConfigDict(extra="ignore")

    symbol: str  # e.g. "BTC/USDT" or "BTCUSDT"
    action: str  # "BUY", "SELL", "LONG", "SHORT", "CLOSE"
    market_type: str = "FUTURES_USDT"
    strategy_id: str | None = None
    signal_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit_1: Decimal | None = None
    take_profit_2: Decimal | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WebhookSignalReceiver:
    """Validates HMAC signatures, checks replay windows, and publishes TradeSignalEvents."""

    def __init__(
        self,
        event_bus: EventBus,
        webhook_secret: str,
        max_skew_seconds: int = 300,
    ) -> None:
        self.event_bus = event_bus
        self.webhook_secret = webhook_secret
        self.max_skew_seconds = max_skew_seconds

    def verify_request(
        self,
        timestamp: int,
        raw_body: bytes,
        signature: str,
    ) -> bool:
        """Verify HMAC-SHA256 signature and replay freshness."""
        return verify_webhook_signature(
            secret=self.webhook_secret,
            timestamp=timestamp,
            raw_body=raw_body,
            provided_signature=signature,
            max_skew_seconds=self.max_skew_seconds,
        )

    async def process_signal(
        self,
        payload_data: dict[str, Any],
    ) -> TradeSignalEvent:
        """Normalize validated webhook payload and publish TradeSignalEvent to EventBus."""
        model = WebhookSignalPayload.model_validate(payload_data)

        # Normalize symbol
        sym = model.symbol.upper().strip()
        if "/" not in sym and sym.endswith("USDT"):
            sym = f"{sym[:-4]}/USDT"

        # Normalize side
        act = model.action.upper().strip()
        side = PositionSide.LONG if act in ("BUY", "LONG") else PositionSide.SHORT

        market_type = (
            MarketType.SPOT
            if model.market_type.upper() == "SPOT"
            else MarketType.FUTURES_USDT
        )

        sig_price = model.signal_price or Decimal("0")
        stop_loss = model.stop_loss or (
            sig_price * Decimal("0.98") if side == PositionSide.LONG else sig_price * Decimal("1.02")
        )

        tp_targets: list[TakeProfitTargetSpec] = []
        if model.take_profit_1:
            tp_targets.append(
                TakeProfitTargetSpec(
                    target_index=1,
                    price=model.take_profit_1,
                    close_Dynamic_pct=Decimal("50") if model.take_profit_2 else Decimal("100"),
                    move_sl_to_breakeven=True,
                )
            )
        if model.take_profit_2:
            tp_targets.append(
                TakeProfitTargetSpec(
                    target_index=2,
                    price=model.take_profit_2,
                    close_Dynamic_pct=Decimal("50"),
                    move_sl_to_breakeven=False,
                )
            )

        signal_event = TradeSignalEvent(
            strategy_id=model.strategy_id or "webhook-external",
            source=SignalSource.WEBHOOK,
            symbol=sym,
            market_type=market_type,
            side=side,
            signal_price=sig_price,
            stop_loss_price=stop_loss,
            take_profit_targets=tp_targets,
            metadata=model.metadata,
        )

        await self.event_bus.publish(signal_event)
        logger.info(
            "Dispatched Webhook Signal for %s %s at %s",
            sym,
            side.value,
            sig_price,
        )
        return signal_event
