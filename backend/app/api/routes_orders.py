"""Order History, Fills, and Order Cancellation API."""

from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_db_session, get_strategy_runner
from app.domains.cex.execution.runner import ContinuousStrategyRunner
from app.models.entities import Order, Position, StrategyConfig
from app.models.enums import TradingEnvironment

router = APIRouter(prefix="/orders", tags=["Orders"])


@router.get("")
async def list_orders(
    limit: int = 100,
    environment: str | None = None,
    db: AsyncSession = Depends(get_db_session),
    runner: ContinuousStrategyRunner = Depends(get_strategy_runner),
) -> list[dict[str, Any]]:
    """Recent orders on one environment's Binance account (default: the active one)."""
    try:
        env = TradingEnvironment(environment.upper().strip()) if environment else runner.environment
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="environment must be DEMO or LIVE") from exc

    strat_stmt = select(StrategyConfig)
    strats = {s.id: s.name for s in (await db.execute(strat_stmt)).scalars().all()}

    stmt = (
        select(Order, Position.strategy_id)
        .join(Position, Position.id == Order.position_id)
        .where(Position.execution_mode == env.value)
        .options(selectinload(Order.fills))
        .order_by(Order.created_at.desc())
        .limit(min(limit, 200))
    )
    rows = (await db.execute(stmt)).all()
    pos_map = {o.position_id: strategy_id for o, strategy_id in rows}

    def get_order_strat(o: Order) -> tuple[str, str]:
        name = strats.get(pos_map.get(o.position_id), "") or "Manual / Unknown"
        abbr = "DCA" if "DCA" in name else ("EMA" if "EMA" in name else ("BB" if "Breakout" in name or "BB" in name else "STRAT"))
        return name, abbr

    orders = [o for o, _ in rows]
    results = []
    for o in orders:
        s_name, s_abbr = get_order_strat(o)
        results.append({
            "id": o.id,
            "position_id": o.position_id,
            "strategy_name": s_name,
            "strategy_abbr": s_abbr,
            "client_order_id": o.client_order_id,
            "exchange_order_id": o.exchange_order_id,
            "symbol": o.symbol,
            "market_type": o.market_type,
            "purpose": o.purpose,
            "order_type": o.order_type,
            "side": o.side,
            "status": o.status,
            "requested_qty": float(o.requested_qty),
            "filled_qty": float(o.filled_qty),
            "avg_fill_price": float(o.avg_fill_price) if o.avg_fill_price else None,
            "fee_usd": float(o.fee_usd),
            "created_at": o.created_at.isoformat(),
            "fills": [
                {
                    "trade_id": f.exchange_trade_id,
                    "price": float(f.fill_price),
                    "qty": float(f.fill_qty),
                    "fee": float(f.fee_amount),
                    "currency": f.fee_currency,
                }
                for f in o.fills
            ],
        })
    return results
