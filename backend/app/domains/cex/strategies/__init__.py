"""Unified Strategy Framework, Built-In Strategies, and Webhook Receiver."""

from app.domains.cex.strategies.base import IStrategy
from app.domains.cex.strategies.bb_breakout import BollingerAdxBreakoutStrategy
from app.domains.cex.strategies.ema_rsi_atr import EmaRsiAtrTrendStrategy
from app.domains.cex.strategies.indicators import (
    calculate_adx,
    calculate_atr,
    calculate_bollinger_bands,
    calculate_ema,
    calculate_rsi,
)
from app.domains.cex.strategies.registry import StrategyRegistry
from app.domains.cex.strategies.smart_dca import SmartDcaScaleInStrategy
from app.domains.cex.strategies.webhook_receiver import (
    WebhookSignalPayload,
    WebhookSignalReceiver,
)

__all__ = [
    "BollingerAdxBreakoutStrategy",
    "EmaRsiAtrTrendStrategy",
    "IStrategy",
    "SmartDcaScaleInStrategy",
    "StrategyRegistry",
    "WebhookSignalPayload",
    "WebhookSignalReceiver",
    "calculate_adx",
    "calculate_atr",
    "calculate_bollinger_bands",
    "calculate_ema",
    "calculate_rsi",
]
