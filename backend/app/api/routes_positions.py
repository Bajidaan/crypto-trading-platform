"""Position Monitoring and Emergency Close API."""

from decimal import Decimal
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, get_strategy_runner
from app.domains.cex.execution.runner import ContinuousStrategyRunner
from app.models.entities import Position, StrategyConfig
from app.models.enums import MarketType, PositionStatus, TradingEnvironment

router = APIRouter(prefix="/positions", tags=["Positions"])


def _resolve_environment(environment: str | None, runner: ContinuousStrategyRunner) -> TradingEnvironment:
    """The environment a request asks about; defaults to the active one."""
    if environment is None:
        return runner.environment
    try:
        return TradingEnvironment(environment.upper().strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="environment must be DEMO or LIVE") from exc


@router.get("/analytics")
async def get_positions_analytics(
    environment: str | None = None,
    db: AsyncSession = Depends(get_db_session),
    runner: ContinuousStrategyRunner = Depends(get_strategy_runner),
) -> dict[str, Any]:
    """Post-trade analytics, strategy attribution and journal for one environment's account."""
    env = _resolve_environment(environment, runner)
    # 1. Fetch strategies to map IDs to friendly names
    strat_stmt = select(StrategyConfig)
    strats = {s.id: s.name for s in (await db.execute(strat_stmt)).scalars().all()}

    # 2. Fetch the environment's closed positions
    stmt = (
        select(Position)
        .where(Position.status == "CLOSED", Position.execution_mode == env.value)
        .order_by(Position.closed_at.desc())
    )
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
    environment: str | None = None,
    db: AsyncSession = Depends(get_db_session),
    runner: ContinuousStrategyRunner = Depends(get_strategy_runner),
) -> list[dict[str, Any]]:
    """Recent positions of one environment's Binance account (default: the active one).

    Open positions are marked to the live price from the kline stream.
    """
    env = _resolve_environment(environment, runner)
    strat_stmt = select(StrategyConfig)
    strats = {s.id: s.name for s in (await db.execute(strat_stmt)).scalars().all()}

    def get_strat_meta(s_id: str | None) -> tuple[str, str]:
        name = strats.get(s_id, "") or "Manual / Unknown"
        abbr = "DCA" if "DCA" in name else ("EMA" if "EMA" in name else ("BB" if "Breakout" in name or "BB" in name else "STRAT"))
        return name, abbr

    stmt = (
        select(Position)
        .where(Position.execution_mode == env.value)
        .order_by(Position.opened_at.desc())
        .limit(100)
    )
    db_positions = (await db.execute(stmt)).scalars().all()

    results: list[dict[str, Any]] = []
    for p in db_positions:
        s_name, s_abbr = get_strat_meta(p.strategy_id)
        is_open = p.status == PositionStatus.OPEN.value
        mark = runner.last_price(p.symbol, MarketType(p.market_type)) if is_open else None
        cur_price = float(mark) if mark is not None else float(p.entry_price)

        if is_open and mark is not None:
            direction = Decimal("1") if p.side == "LONG" else Decimal("-1")
            unrealized = (mark - p.entry_price) * p.current_qty * direction
        else:
            unrealized = p.unrealized_pnl_usd or Decimal("0")

        tp_price: float | None = None
        if p.take_profit_state and isinstance(p.take_profit_state, list) and p.take_profit_state:
            first_tp = p.take_profit_state[0]
            if isinstance(first_tp, dict) and "price" in first_tp:
                tp_price = float(first_tp["price"])

        notional_usd = float(p.entry_price) * float(p.current_qty)
        unrealized_usd = float(unrealized)
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
    return results


@router.post("/{position_id}/close")
async def close_position(
    position_id: str,
    db: AsyncSession = Depends(get_db_session),
    runner: ContinuousStrategyRunner = Depends(get_strategy_runner),
) -> dict[str, str]:
    """Flatten a position on the Binance account it lives on (cancels its stop first)."""
    pos = await db.get(Position, position_id)
    if not pos:
        raise HTTPException(status_code=404, detail="Position not found.")
    if pos.status != PositionStatus.OPEN.value:
        raise HTTPException(status_code=400, detail="Position is already closed.")
    if pos.execution_mode not in (TradingEnvironment.DEMO.value, TradingEnvironment.LIVE.value):
        raise HTTPException(status_code=400, detail="Paper positions from the removed simulator cannot be closed.")
    if runner.live_broker is None:
        raise HTTPException(status_code=503, detail="Exchange broker is not configured.")

    adapter = await runner._get_live_adapter(pos.exchange_connection_id)
    if adapter is None:
        raise HTTPException(
            status_code=409,
            detail="This position's API key is missing or disabled; close it on Binance directly.",
        )
    closed = await runner.live_broker.close_position(adapter, position_id, exit_reason="MANUAL_CLOSE")
    if not closed:
        raise HTTPException(status_code=502, detail="Binance did not accept the close order; see risk alerts.")
    return {"status": "CLOSED", "position_id": position_id, "mode": pos.execution_mode}
