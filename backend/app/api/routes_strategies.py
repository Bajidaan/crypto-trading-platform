"""Strategy Management, Tuning, and Backtesting Execution API."""

from decimal import Decimal
from typing import Any
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_adapter_factory, get_db_session
from app.domains.cex.adapters.factory import ExchangeAdapterFactory
from app.domains.cex.backtesting import DeterministicBacktestEngine, fetch_ohlcv_history
from app.domains.cex.market_data.buffer import timeframe_to_seconds
from app.domains.cex.strategies.registry import StrategyRegistry
from app.models.entities import BacktestRun, ExchangeConnection, StrategyConfig
from app.models.enums import ExecutionMode, MarketType

router = APIRouter(prefix="/strategies", tags=["Strategies"])


class CreateStrategyRequest(BaseModel):
    name: str
    strategy_class: str  # "ema_rsi_atr", "bb_breakout", "smart_dca"
    execution_mode: str = "PAPER"
    market_type: str = "FUTURES_USDT"
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT"])
    timeframe: str = "15m"
    exchange_connection_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    risk_config: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class UpdateStrategyRequest(BaseModel):
    is_active: bool | None = None
    is_hidden: bool | None = None
    timeframe: str | None = None
    symbols: list[str] | None = None
    execution_mode: str | None = None
    market_type: str | None = None
    parameters: dict[str, Any] | None = None
    risk_config: dict[str, Any] | None = None


class RunBacktestRequest(BaseModel):
    symbol: str = "BTC/USDT"
    # Defaults to the strategy's own timeframe.
    timeframe: str | None = None
    # Length of the tested window: give one of these, or neither for a default
    # (1500 bars on 4h-or-longer timeframes, 500 bars otherwise). The strategy's warmup
    # bars are fetched in addition, so the whole window is tradable.
    lookback_days: int | None = Field(default=None, ge=1)
    candle_limit: int | None = Field(default=None, ge=10)
    initial_capital: float = 10000.0
    risk_per_trade_pct: float = 1.0
    # Per-side costs in percent. cost_multiplier scales both, e.g. 2.0 for a stress test.
    taker_fee_pct: float = Field(default=0.05, ge=0)
    slippage_pct: float = Field(default=0.05, ge=0)
    cost_multiplier: float = Field(default=1.0, ge=0)


@router.get("/available-classes")
async def list_available_strategy_classes() -> list[str]:
    return StrategyRegistry.list_available()


@router.get("")
async def list_strategies(
    include_hidden: bool = False,
    db: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    stmt = select(StrategyConfig).order_by(StrategyConfig.created_at.desc())
    if not include_hidden:
        stmt = stmt.where(StrategyConfig.is_hidden == False)  # noqa: E712
    strats = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "strategy_class": s.strategy_class,
            "execution_mode": s.execution_mode,
            "market_type": s.market_type,
            "symbols": s.symbols,
            "timeframe": s.timeframe,
            "exchange_connection_id": s.exchange_connection_id,
            "parameters": s.parameters,
            "risk_config": s.risk_config,
            "is_active": s.is_active,
            "is_hidden": s.is_hidden,
            "created_at": s.created_at.isoformat(),
        }
        for s in strats
    ]


@router.post("")
async def create_strategy(
    req: CreateStrategyRequest,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    # Validate strategy class exists
    if req.strategy_class.lower() not in StrategyRegistry.list_available():
        raise HTTPException(
            status_code=400,
            detail=f"Invalid strategy_class '{req.strategy_class}'. Available: {StrategyRegistry.list_available()}",
        )

    strat_id = str(uuid.uuid4())
    config = StrategyConfig(
        id=strat_id,
        name=req.name,
        strategy_class=req.strategy_class.lower().strip(),
        execution_mode=req.execution_mode.upper().strip(),
        market_type=req.market_type.upper().strip(),
        symbols=req.symbols,
        timeframe=req.timeframe,
        exchange_connection_id=req.exchange_connection_id,
        parameters=req.parameters,
        risk_config=req.risk_config,
        is_active=req.is_active,
    )
    db.add(config)
    await db.commit()

    return {
        "id": config.id,
        "name": config.name,
        "strategy_class": config.strategy_class,
        "is_active": config.is_active,
    }


@router.patch("/{strategy_id}")
async def update_strategy(
    strategy_id: str,
    req: UpdateStrategyRequest,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    stmt = select(StrategyConfig).where(StrategyConfig.id == strategy_id)
    strat = (await db.execute(stmt)).scalar_one_or_none()
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    if req.is_hidden is not None:
        strat.is_hidden = req.is_hidden
        if req.is_hidden:
            strat.is_active = False  # a hidden strategy must never keep running unseen
    if req.is_active:
        if strat.is_hidden:
            raise HTTPException(status_code=400, detail="A hidden strategy cannot be activated; unhide it first.")
        strat.is_active = True
    elif req.is_active is not None:
        strat.is_active = False
    if req.timeframe is not None:
        strat.timeframe = req.timeframe.strip()
    if req.symbols is not None:
        strat.symbols = req.symbols
    if req.execution_mode is not None:
        strat.execution_mode = req.execution_mode.upper().strip()
    if req.market_type is not None:
        strat.market_type = req.market_type.upper().strip()
    if req.parameters is not None:
        strat.parameters = {**strat.parameters, **req.parameters}
    if req.risk_config is not None:
        strat.risk_config = {**strat.risk_config, **req.risk_config}

    await db.commit()
    return {
        "id": strat.id,
        "name": strat.name,
        "strategy_class": strat.strategy_class,
        "execution_mode": strat.execution_mode,
        "market_type": strat.market_type,
        "symbols": strat.symbols,
        "timeframe": strat.timeframe,
        "exchange_connection_id": strat.exchange_connection_id,
        "parameters": strat.parameters,
        "risk_config": strat.risk_config,
        "is_active": strat.is_active,
        "is_hidden": strat.is_hidden,
        "created_at": strat.created_at.isoformat(),
    }


@router.delete("/{strategy_id}")
async def delete_strategy(
    strategy_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    stmt = select(StrategyConfig).where(StrategyConfig.id == strategy_id)
    strat = (await db.execute(stmt)).scalar_one_or_none()
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    await db.delete(strat)
    await db.commit()
    return {"status": "DELETED", "id": strategy_id}


@router.post("/{strategy_id}/backtest")
async def run_strategy_backtest(
    strategy_id: str,
    req: RunBacktestRequest,
    db: AsyncSession = Depends(get_db_session),
    factory: ExchangeAdapterFactory = Depends(get_adapter_factory),
) -> dict[str, Any]:
    stmt = select(StrategyConfig).where(StrategyConfig.id == strategy_id)
    strat_cfg = (await db.execute(stmt)).scalar_one_or_none()
    if not strat_cfg:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    # Instantiate strategy
    strategy_instance = StrategyRegistry.create_instance(strat_cfg)

    # Use first available exchange connection or testnet public client to fetch historical bars
    conn_stmt = select(ExchangeConnection).where(ExchangeConnection.is_enabled == True).limit(1)
    conn = (await db.execute(conn_stmt)).scalar_one_or_none()

    if conn:
        adapter = factory.create_from_connection(conn)
    else:
        # Default fallback to Binance public adapter
        from app.domains.cex.adapters.binance import BinanceAdapter
        adapter = BinanceAdapter(api_key="", api_secret="", is_testnet=False)

    timeframe = req.timeframe or strat_cfg.timeframe or "15m"
    strategy_instance.timeframe = timeframe
    tf_seconds = timeframe_to_seconds(timeframe)
    if req.lookback_days is not None:
        window_bars = max(10, req.lookback_days * 86400 // tf_seconds)
    elif req.candle_limit is not None:
        window_bars = req.candle_limit
    else:
        window_bars = 1500 if tf_seconds >= 4 * 3600 else 500
    total_bars = window_bars + strategy_instance.required_warmup_bars

    context_candles: dict[str, list[Any]] = {}
    try:
        m_type = MarketType(strat_cfg.market_type)
        candles = await fetch_ohlcv_history(adapter, req.symbol, m_type, timeframe, total_bars)
        for ctx_symbol in strategy_instance.context_symbols:
            if ctx_symbol != req.symbol:
                context_candles[ctx_symbol] = await fetch_ohlcv_history(
                    adapter, ctx_symbol, m_type, timeframe, total_bars
                )
    finally:
        await adapter.close()

    if not candles:
        raise HTTPException(status_code=400, detail="Failed to retrieve historical candle data.")

    cost_scale = Decimal(str(req.cost_multiplier)) / Decimal("100")
    engine = DeterministicBacktestEngine(
        strategy=strategy_instance,
        initial_capital=Decimal(str(req.initial_capital)),
        risk_per_trade_pct=Decimal(str(req.risk_per_trade_pct)),
        taker_fee=Decimal(str(req.taker_fee_pct)) * cost_scale,
        slippage_pct=Decimal(str(req.slippage_pct)) * cost_scale,
    )
    result = engine.run(candles, context_candles=context_candles)
    comparison = result.comparison.model_dump(mode="json") if result.comparison else {}

    metrics_dict = result.metrics.model_dump(mode="json")
    trades_sanitized = [
        {k: (float(v) if isinstance(v, Decimal) else v) for k, v in t.items()}
        for t in result.trades
    ]

    # Save BacktestRun
    run_record = BacktestRun(
        id=str(uuid.uuid4()),
        strategy_class=strat_cfg.strategy_class,
        exchange_id="BINANCE",
        symbol=req.symbol,
        market_type=strat_cfg.market_type,
        timeframe=timeframe,
        start_date=candles[0].open_time,
        end_date=candles[-1].open_time,
        initial_capital_usd=result.metrics.initial_capital,
        parameters=strat_cfg.parameters,
        metrics={**metrics_dict, "comparison": comparison},
        equity_curve=[{"index": i, "equity": float(x)} for i, x in enumerate(result.equity_curve)],
        trades_log=trades_sanitized,
    )
    db.add(run_record)
    await db.commit()

    return {
        "backtest_id": run_record.id,
        "symbol": req.symbol,
        "total_trades": len(result.trades),
        "total_return_pct": float(metrics_dict.get("total_return_pct", 0.0)),
        "win_rate_pct": float(metrics_dict.get("win_rate_pct", 0.0)),
        "profit_factor": float(metrics_dict.get("profit_factor", 0.0)),
        # Drawdown is marked to market at every bar, so open-trade losses are included.
        "max_drawdown_pct": float(comparison.get("strategy_max_drawdown_pct", 0.0)),
        "timeframe": timeframe,
        "bars": len(candles),
        "comparison": comparison,
        "metrics": metrics_dict,
        "trades_count": len(result.trades),
        "equity_curve": [float(x) for x in result.equity_curve],
    }
