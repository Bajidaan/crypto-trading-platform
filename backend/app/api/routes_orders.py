"""Order History, Fills, and Order Cancellation API."""

from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_db_session
from app.models.entities import Order, Position, StrategyConfig

router = APIRouter(prefix="/orders", tags=["Orders"])


@router.get("")
async def list_orders(
    limit: int = 100,
    db: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    # Map positions to strategy names
    strat_stmt = select(StrategyConfig)
    strats = {s.id: s.name for s in (await db.execute(strat_stmt)).scalars().all()}
    pos_stmt = select(Position)
    pos_map = {p.id: p.strategy_id for p in (await db.execute(pos_stmt)).scalars().all()}

    def get_order_strat(o: Order) -> tuple[str, str]:
        s_id = pos_map.get(o.position_id)
        name = strats.get(s_id, "")
        if not name:
            cid = o.client_order_id or ""
            if "1e678841" in cid:
                name = "Smart DCA Scale-In"
            elif "0efe0031" in cid:
                name = "BTC/ETH EMA RSI Trend"
            elif "ba77b92f" in cid:
                name = "SOL Volatility Breakout"
            else:
                name = "Smart DCA Scale-In"
        abbr = "DCA" if "DCA" in name else ("EMA" if "EMA" in name else ("BB" if "Breakout" in name or "BB" in name else "STRAT"))
        return name, abbr

    stmt = (
        select(Order)
        .options(selectinload(Order.fills))
        .order_by(Order.created_at.desc())
        .limit(min(limit, 200))
    )
    orders = (await db.execute(stmt)).scalars().all()
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
