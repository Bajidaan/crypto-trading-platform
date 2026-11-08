"""Position Monitoring and Emergency Close API."""

from decimal import Decimal
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_adapter_factory, get_db_session, get_paper_broker
from app.domains.cex.adapters.factory import ExchangeAdapterFactory
from app.domains.cex.execution.paper_broker import PaperTradingBroker
from app.models.entities import ExchangeConnection, Position, StrategyConfig
from app.models.enums import ExecutionMode, MarketType, PositionSide, PositionStatus

router = APIRouter(prefix="/positions", tags=["Positions"])


@router.get("/analytics")
async def get_positions_analytics(
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Provide structured post-trade analytics, strategy attribution, and outcome journal."""
    # 1. Fetch strategies to map IDs to friendly names
    strat_stmt = select(StrategyConfig)
    strats = {s.id: s.name for s in (await db.execute(strat_stmt)).scalars().all()}

    # 2. Fetch all closed positions
    stmt = select(Position).where(Position.status == "CLOSED").order_by(Position.closed_at.desc())
    closed_pos = list((await db.execute(stmt)).scalars().all())

    total_trades = len(closed_pos)
    winning_trades = sum(1 for p in closed_pos if p.realized_pnl_usd > 0)
    losing_trades = sum(1 for p in closed_pos if p.realized_pnl_usd < 0)
    breakeven_trades = sum(1 for p in closed_pos if p.realized_pnl_usd == 0)

    win_rate_pct = round((winning_trades / total_trades * 100.0), 2) if total_trades > 0 else 0.0
    total_realized_pnl = sum((p.realized_pnl_usd for p in closed_pos), Decimal("0"))
    gross_profit = sum((p.realized_pnl_usd for p in closed_pos if p.realized_pnl_usd > 0), Decimal("0"))
    gross_loss = abs(sum((p.realized_pnl_usd for p in closed_pos if p.realized_pnl_usd < 0), Decimal("0")))
    profit_factor = round(float(gross_profit / gross_loss), 2) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    total_fees = sum((p.total_fees_usd for p in closed_pos), Decimal("0"))

    # Strategy breakdown
    strat_stats: dict[str, dict[str, Any]] = {}
    for p in closed_pos:
        s_name = strats.get(p.strategy_id, "Unknown Strategy")
        if s_name not in strat_stats:
            strat_stats[s_name] = {
                "strategy_name": s_name,
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "total_pnl": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "exit_reasons": {},
            }
        st = strat_stats[s_name]
        st["total_trades"] += 1
        pnl = float(p.realized_pnl_usd)
        st["total_pnl"] += pnl
        if pnl > 0:
            st["winning_trades"] += 1
            st["gross_profit"] += pnl
        elif pnl < 0:
            st["losing_trades"] += 1
            st["gross_loss"] += abs(pnl)

        reason = p.exit_reason or "MANUAL_CLOSE"
        st["exit_reasons"][reason] = st["exit_reasons"].get(reason, 0) + 1

    # Finalize strategy stats
    strategy_attribution = []
    for s_name, st in strat_stats.items():
        total_s = st["total_trades"]
        wr = round((st["winning_trades"] / total_s * 100.0), 2) if total_s > 0 else 0.0
        pf = round(st["gross_profit"] / st["gross_loss"], 2) if st["gross_loss"] > 0 else (999.0 if st["gross_profit"] > 0 else 0.0)
        strategy_attribution.append({
            "strategy_name": s_name,
            "total_trades": total_s,
            "win_rate_pct": wr,
            "realized_pnl_usd": round(st["total_pnl"], 2),
            "profit_factor": pf,
            "exit_reasons": st["exit_reasons"],
        })

    # Detailed trades journal
    journal_trades = []
    cum_pnl = 0.0
    equity_curve = []
    for p in reversed(closed_pos):
        pnl = float(p.realized_pnl_usd)
        cum_pnl += pnl
        equity_curve.append({
            "time": p.closed_at.isoformat() if p.closed_at else (p.opened_at.isoformat() if p.opened_at else ""),
            "cumulative_pnl": round(cum_pnl, 2),
        })

    for p in closed_pos:
        dur_seconds = (p.closed_at - p.opened_at).total_seconds() if p.closed_at and p.opened_at else 0
        dur_str = f"{int(dur_seconds // 60)}m {int(dur_seconds % 60)}s" if dur_seconds < 3600 else f"{int(dur_seconds // 3600)}h {int((dur_seconds % 3600) // 60)}m"

        notional = float(p.entry_price * p.initial_qty)
        pnl_pct = (float(p.realized_pnl_usd) / notional * 100.0 * p.leverage) if notional > 0 else 0.0

        gross_pnl = p.realized_pnl_usd + (p.total_fees_usd or Decimal("0"))
        price_delta = (gross_pnl / p.initial_qty) if p.initial_qty > 0 else Decimal("0")
        calc_exit_price = (p.entry_price + price_delta) if p.side == "LONG" else (p.entry_price - price_delta)

        journal_trades.append({
            "id": p.id,
            "strategy_name": strats.get(p.strategy_id, "Smart DCA / Trend"),
            "symbol": p.symbol,
            "market_type": p.market_type,
            "side": p.side,
            "leverage": p.leverage,
            "entry_price": float(p.entry_price),
            "exit_price": float(calc_exit_price),
            "quantity": float(p.initial_qty),
            "realized_pnl_usd": float(p.realized_pnl_usd),
            "realized_pnl_pct": round(pnl_pct, 2),
            "fees_usd": float(p.total_fees_usd),
            "exit_reason": p.exit_reason or "TAKE_PROFIT",
            "duration": dur_str,
            "opened_at": p.opened_at.isoformat() if p.opened_at else "",
            "closed_at": p.closed_at.isoformat() if p.closed_at else "",
        })

    return {
        "summary": {
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "breakeven_trades": breakeven_trades,
            "win_rate_pct": win_rate_pct,
            "total_realized_pnl_usd": round(float(total_realized_pnl), 2),
            "profit_factor": profit_factor,
            "total_fees_usd": round(float(total_fees), 2),
        },
        "strategy_attribution": strategy_attribution,
        "equity_curve": equity_curve,
        "trades": journal_trades,
    }


@router.get("")
async def list_positions(
    db: AsyncSession = Depends(get_db_session),
    paper_broker: PaperTradingBroker = Depends(get_paper_broker),
) -> list[dict[str, Any]]:
    # Fetch strategies to map IDs to friendly names and abbreviations
    strat_stmt = select(StrategyConfig)
    strats = {s.id: s.name for s in (await db.execute(strat_stmt)).scalars().all()}

    def get_strat_meta(s_id: str | None) -> tuple[str, str]:
        name = strats.get(s_id, "")
        if not name:
            s_str = str(s_id) if s_id else ""
            if "1e678841" in s_str:
                name = "Smart DCA Scale-In"
            elif "0efe0031" in s_str:
                name = "BTC/ETH EMA RSI Trend"
            elif "ba77b92f" in s_str:
                name = "SOL Volatility Breakout"
            else:
                name = "Smart DCA Scale-In"
        abbr = "DCA" if "DCA" in name else ("EMA" if "EMA" in name else ("BB" if "Breakout" in name or "BB" in name else "STRAT"))
        return name, abbr

    # Map in-memory paper positions for live mark price & TP lookup
    paper_map = {pp.position_id: pp for pp in paper_broker.positions}

    # 1. Fetch DB positions (Live & historical)
    stmt = select(Position).order_by(Position.opened_at.desc()).limit(100)
    db_positions = (await db.execute(stmt)).scalars().all()

    results: list[dict[str, Any]] = []
    for p in db_positions:
        s_name, s_abbr = get_strat_meta(p.strategy_id)
        pp_live = paper_map.get(p.id)

        # Calculate accurate current/mark price without double-adding entry_price on SHORT
        if pp_live and getattr(pp_live, "current_price", None):
            cur_price = float(pp_live.current_price)
        elif float(p.current_qty) > 0 and float(p.unrealized_pnl_usd) != 0.0:
            if p.side == "LONG":
                cur_price = float(p.entry_price) + (float(p.unrealized_pnl_usd) / float(p.current_qty))
            else:
                cur_price = float(p.entry_price) - (float(p.unrealized_pnl_usd) / float(p.current_qty))
        else:
            cur_price = float(p.entry_price)

        # Resolve take profit price from stored strategy targets first
        tp_price: float | None = None
        if p.take_profit_state and isinstance(p.take_profit_state, list) and len(p.take_profit_state) > 0:
            first_tp = p.take_profit_state[0]
            if isinstance(first_tp, dict) and "price" in first_tp:
                tp_price = float(first_tp["price"])
        if tp_price is None and pp_live and pp_live.tp_targets:
            tp_price = float(pp_live.tp_targets[0].price)
        if tp_price is None:
            tp_price = float(p.entry_price * Decimal("1.04")) if p.side == "LONG" else float(p.entry_price * Decimal("0.96"))

        unrealized_usd = float(pp_live.unrealized_pnl) if pp_live else float(p.unrealized_pnl_usd)
        notional_usd = float(p.entry_price) * float(p.current_qty)

        results.append({
            "id": p.id,
            "strategy_id": p.strategy_id,
            "strategy_name": s_name,
            "strategy_abbr": s_abbr,
            "symbol": p.symbol,
            "market_type": p.market_type,
            "execution_mode": p.execution_mode,
            "side": p.side,
            "margin_mode": p.margin_mode or "ISOLATED",
            "leverage": p.leverage,
            "entry_price": float(p.entry_price),
            "current_price": cur_price,
            "current_qty": float(p.current_qty),
            "stop_loss_price": float(p.stop_loss_price) if p.stop_loss_price else None,
            "take_profit_price": tp_price,
            "stop_order_live": p.stop_order_live,
            "unrealized_pnl_usd": unrealized_usd,
            "unrealized_pnl_pct": (unrealized_usd / notional_usd * 100 * p.leverage) if notional_usd > 0 else 0.0,
            "realized_pnl_usd": float(p.realized_pnl_usd),
            "exit_reason": p.exit_reason,
            "status": p.status,
            "opened_at": p.opened_at.isoformat(),
        })

    # 2. Append active paper positions (deduplicate against DB records)
    db_ids = {p.id for p in db_positions}
    for pp in paper_broker.positions:
        if pp.position_id in db_ids:
            continue
        s_name, s_abbr = get_strat_meta(pp.strategy_id)
        tp_val = (
            float(pp.tp_targets[0].price)
            if pp.tp_targets
            else (float(pp.entry_price * Decimal("1.04")) if pp.side.value == "LONG" else float(pp.entry_price * Decimal("0.96")))
        )
        notional_pp = float(pp.entry_price * pp.current_qty)
        unrealized_pp = float(getattr(pp, "unrealized_pnl", Decimal("0")))
        results.insert(
            0,
            {
                "id": pp.position_id,
                "strategy_id": pp.strategy_id,
                "strategy_name": s_name,
                "strategy_abbr": s_abbr,
                "symbol": pp.symbol,
                "market_type": pp.market_type.value,
                "execution_mode": ExecutionMode.PAPER.value,
                "side": pp.side.value,
                "margin_mode": "ISOLATED",
                "leverage": pp.leverage,
                "entry_price": float(pp.entry_price),
                "current_price": float(getattr(pp, "current_price", pp.entry_price)),
                "current_qty": float(pp.current_qty),
                "stop_loss_price": float(pp.stop_loss_price) if pp.stop_loss_price else None,
                "take_profit_price": tp_val,
                "stop_order_live": pp.stop_order_live,
                "unrealized_pnl_usd": unrealized_pp,
                "unrealized_pnl_pct": (unrealized_pp / notional_pp * 100 * pp.leverage) if notional_pp > 0 else 0.0,
                "realized_pnl_usd": float(pp.realized_pnl),
                "exit_reason": getattr(pp, "exit_reason", None),
                "status": pp.status.value,
                "opened_at": pp.created_at.isoformat(),
            },
        )

    return results


@router.post("/{position_id}/close")
async def close_position(
    position_id: str,
    db: AsyncSession = Depends(get_db_session),
    factory: ExchangeAdapterFactory = Depends(get_adapter_factory),
    paper_broker: PaperTradingBroker = Depends(get_paper_broker),
) -> dict[str, str]:
    # Check paper positions first
    if await paper_broker.close_position_market(position_id, exit_reason="MANUAL_CLOSE"):
        return {"status": "CLOSED", "position_id": position_id, "mode": "PAPER"}

    # Check live DB positions
    stmt = select(Position).where(Position.id == position_id)
    pos = (await db.execute(stmt)).scalar_one_or_none()
    if not pos:
        raise HTTPException(status_code=404, detail="Position not found.")

    if pos.status != PositionStatus.OPEN.value:
        raise HTTPException(status_code=400, detail="Position is already closed.")

    conn_stmt = select(ExchangeConnection).where(ExchangeConnection.id == pos.exchange_connection_id)
    conn = (await db.execute(conn_stmt)).scalar_one_or_none()
    if not conn:
        pos.status = PositionStatus.CLOSED.value
        await db.commit()
        return {"status": "CLOSED", "position_id": position_id}

    adapter = factory.create_from_connection(conn)
    try:
        await adapter.close_position_market(
            symbol=pos.symbol,
            market_type=MarketType(pos.market_type),
            side=PositionSide(pos.side),
            quantity=pos.current_qty,
            client_order_id=f"close_{pos.id[:8]}",
        )
        pos.status = PositionStatus.CLOSED.value
        pos.current_qty = Decimal("0")
        await db.commit()
        return {"status": "CLOSED", "position_id": position_id, "mode": "LIVE"}
    finally:
        await adapter.close()
