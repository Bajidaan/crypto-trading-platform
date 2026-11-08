"""External Signal Webhook Ingestion API."""

import json
from typing import Any
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.api.deps import get_event_bus
from app.config import get_settings
from app.core.events import EventBus
from app.domains.cex.strategies.webhook_receiver import WebhookSignalReceiver

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


@router.post("/signal")
async def receive_tradingview_signal(
    request: Request,
    x_signature: str | None = Header(None, alias="X-Signature"),
    x_timestamp: str | None = Header(None, alias="X-Timestamp"),
    event_bus: EventBus = Depends(get_event_bus),
) -> dict[str, Any]:
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc

    settings = get_settings()
    receiver = WebhookSignalReceiver(
        event_bus=event_bus,
        webhook_secret=settings.webhook_hmac_secret,
        max_skew_seconds=settings.webhook_max_timestamp_skew_seconds,
    )

    # If signature headers are provided, enforce cryptographic verification
    if x_signature and x_timestamp:
        try:
            ts_int = int(x_timestamp)
            receiver.verify_request(ts_int, raw_body, x_signature)
        except Exception as exc:
            raise HTTPException(status_code=401, detail=f"Webhook authentication failed: {exc}") from exc

    sig_event = await receiver.process_signal(payload)
    return {
        "status": "ACCEPTED",
        "symbol": sig_event.symbol,
        "side": sig_event.side.value,
        "signal_price": float(sig_event.signal_price),
        "stop_loss": float(sig_event.stop_loss_price),
    }
