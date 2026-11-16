"""Market Data Query API for Frontend Lightweight Charts and Price Feeds."""

from typing import Any
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_strategy_runner
from app.domains.cex.execution.runner import ContinuousStrategyRunner
from app.models.enums import MarketType

router = APIRouter(prefix="/market", tags=["Market Data"])


@router.get("/candles")
async def get_candles(
    symbol: str = "BTC/USDT",
    timeframe: str = "15m",
    market_type: str = "FUTURES_USDT",
    limit: int = 200,
    runner: ContinuousStrategyRunner = Depends(get_strategy_runner),
) -> list[dict[str, Any]]:
    """Chart candles from the engine's live buffers.

    The first request for a symbol loads history once and starts streaming it; later
    requests are answered from memory, and live updates reach the chart as `candle`
    messages on the dashboard WebSocket. Public market data needs no API key, so no
    exchange credentials are decrypted here.
    """
    try:
        m_type = MarketType(market_type.upper().strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown market_type '{market_type}'") from exc
    return await runner.chart_candles(symbol.upper().strip(), timeframe.strip(), m_type, limit)
