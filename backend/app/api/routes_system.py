"""System Management, Circuit Breakers, Kill Switch, and Audit Trail API."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_db_session,
    get_event_bus,
    get_risk_gatekeeper,
    get_strategy_runner,
)
from app.core.events import EventBus, RiskAlertEvent
from app.domains.cex.execution.runner import ContinuousStrategyRunner
from app.domains.cex.risk.gatekeeper import RiskGatekeeper
from app.models.entities import AuditLog, GlobalRiskState, Position
from app.models.enums import PositionStatus, TradingEnvironment

router = APIRouter(prefix="/system", tags=["System"])


class SystemStatusResponse(BaseModel):
    is_paused: bool
    kill_switch_active: bool
    max_concurrent_positions: int
    max_daily_drawdown_pct: float
    max_leverage_cap: int
    # DEMO (Binance Demo Trading) or LIVE (the real account): where new trades go.
    environment: str
    # Label of the environment's enabled Binance API key; None when none is set up.
    connection_label: str | None
    # USDT-M futures wallet equity of the environment's account (spot USDT as fallback).
    equity_usd: float | None
    balances: dict[str, Any]
    balance_error: str | None
    balance_updated_at: str | None
    open_positions: int
    # Open positions left on the other environment (still protected and synced).
    other_environment_open_positions: int
    active_stream_subscriptions: int
    event_bus_published_count: int


class SwitchEnvironmentRequest(BaseModel):
    environment: TradingEnvironment
    # Must be true to switch to LIVE, so a stray request can never move real money.
    confirm_live: bool = False


async def _open_count(db: AsyncSession, environment: TradingEnvironment, other: bool = False) -> int:
    mode_filter = (
        Position.execution_mode.in_([e.value for e in TradingEnvironment if e != environment])
        if other
        else Position.execution_mode == environment.value
    )
    stmt = select(func.count(Position.id)).where(Position.status == PositionStatus.OPEN.value, mode_filter)
    return int((await db.execute(stmt)).scalar() or 0)


@router.get("/status", response_model=SystemStatusResponse)
async def get_system_status(
    db: AsyncSession = Depends(get_db_session),
    gatekeeper: RiskGatekeeper = Depends(get_risk_gatekeeper),
    runner: ContinuousStrategyRunner = Depends(get_strategy_runner),
    event_bus: EventBus = Depends(get_event_bus),
) -> SystemStatusResponse:
    risk_state = (
        await db.execute(select(GlobalRiskState).where(GlobalRiskState.id == 1))
    ).scalar_one_or_none()

    if risk_state:
        gatekeeper.update_risk_state(risk_state)
    state = gatekeeper.risk_state
    env = runner.environment

    account = runner.account
    if account.get("environment") != env.value:
        account = {}
    balances = account.get("balances") or {}
    equity = None
    for mt in ("FUTURES_USDT", "SPOT"):
        if mt in balances:
            equity = float(balances[mt]["total_equity"])
            break
    conn = await runner.connection_for_environment(env)
    stream = runner.stream_stats or {}

    return SystemStatusResponse(
        is_paused=state.is_paused,
        kill_switch_active=state.kill_switch_active,
        max_concurrent_positions=state.max_concurrent_positions,
        max_daily_drawdown_pct=float(state.max_daily_drawdown_pct),
        max_leverage_cap=state.max_leverage_cap,
        environment=env.value,
        connection_label=conn.label if conn else None,
        equity_usd=equity,
        balances={
            mt: {k: float(v) for k, v in b.items()} for mt, b in balances.items()
        },
        balance_error=account.get("error"),
        balance_updated_at=account.get("updated_at"),
        open_positions=await _open_count(db, env),
        other_environment_open_positions=await _open_count(db, env, other=True),
        active_stream_subscriptions=sum((stream.get("streams") or {}).values()),
        event_bus_published_count=event_bus.stats["published_count"],
    )


@router.post("/environment")
async def switch_environment(
    req: SwitchEnvironmentRequest,
    db: AsyncSession = Depends(get_db_session),
    runner: ContinuousStrategyRunner = Depends(get_strategy_runner),
    event_bus: EventBus = Depends(get_event_bus),
) -> dict[str, Any]:
    """Point new trades at Binance Demo Trading or the real account.

    Open positions on the previous environment are not closed; they stay protected by their
    exchange stops and keep being synced, and strategy exit signals still close them.
    """
    target = req.environment
    if target == TradingEnvironment.LIVE and not req.confirm_live:
        raise HTTPException(status_code=400, detail="Switching to LIVE trades real money; confirm_live must be true.")
    conn = await runner.connection_for_environment(target)
    if conn is None:
        raise HTTPException(
            status_code=400,
            detail=f"No enabled {target.value} Binance API key. Add one under API Keys before switching.",
        )

    risk_state = (await db.execute(select(GlobalRiskState).where(GlobalRiskState.id == 1))).scalar_one()
    previous = risk_state.active_environment
    risk_state.active_environment = target.value
    await db.commit()
    await runner.set_environment(target)

    if previous != target.value:
        left_open = await _open_count(db, target, other=True)
        await event_bus.publish(
            RiskAlertEvent(
                severity="CRITICAL" if target == TradingEnvironment.LIVE else "WARNING",
                rule_name="ENVIRONMENT_SWITCHED",
                message=(
                    f"Trading environment switched {previous} -> {target.value} (API key: {conn.label}). "
                    + (f"{left_open} position(s) remain open on {previous} and stay protected." if left_open else "")
                ).strip(),
            )
        )
    return {"environment": target.value, "previous": previous, "connection_label": conn.label}


@router.post("/pause")
async def pause_trading(
    db: AsyncSession = Depends(get_db_session),
    gatekeeper: RiskGatekeeper = Depends(get_risk_gatekeeper),
    event_bus: EventBus = Depends(get_event_bus),
) -> dict[str, str]:
    risk_state = (
        await db.execute(select(GlobalRiskState).where(GlobalRiskState.id == 1))
    ).scalar_one()
    risk_state.is_paused = True
    await db.commit()
    gatekeeper.risk_state.is_paused = True

    await event_bus.publish(
        RiskAlertEvent(
            severity="WARNING",
            rule_name="GLOBAL_PAUSE",
            message="Trading engine was manually PAUSED by the operator.",
        )
    )
    return {"status": "PAUSED"}


@router.post("/resume")
async def resume_trading(
    db: AsyncSession = Depends(get_db_session),
    gatekeeper: RiskGatekeeper = Depends(get_risk_gatekeeper),
    event_bus: EventBus = Depends(get_event_bus),
) -> dict[str, str]:
    risk_state = (
        await db.execute(select(GlobalRiskState).where(GlobalRiskState.id == 1))
    ).scalar_one()
    if risk_state.kill_switch_active:
        raise HTTPException(status_code=400, detail="Cannot resume while Kill Switch is active. Deactivate Kill Switch first.")

    risk_state.is_paused = False
    await db.commit()
    gatekeeper.risk_state.is_paused = False

    await event_bus.publish(
        RiskAlertEvent(
            severity="INFO",
            rule_name="GLOBAL_RESUME",
            message="Trading engine was RESUMED by the operator.",
        )
    )
    return {"status": "ACTIVE"}


@router.post("/kill-switch")
async def activate_kill_switch(
    db: AsyncSession = Depends(get_db_session),
    gatekeeper: RiskGatekeeper = Depends(get_risk_gatekeeper),
    event_bus: EventBus = Depends(get_event_bus),
) -> dict[str, str]:
    risk_state = (
        await db.execute(select(GlobalRiskState).where(GlobalRiskState.id == 1))
    ).scalar_one()
    risk_state.kill_switch_active = True
    risk_state.is_paused = True
    await db.commit()
    gatekeeper.risk_state.kill_switch_active = True
    gatekeeper.risk_state.is_paused = True

    await event_bus.publish(
        RiskAlertEvent(
            severity="CRITICAL",
            rule_name="KILL_SWITCH_ACTIVATED",
            message="EMERGENCY KILL SWITCH ACTIVATED. All entry trading blocked.",
        )
    )
    return {"status": "KILL_SWITCH_ACTIVE"}


@router.get("/audit-logs")
async def list_audit_logs(
    limit: int = 50,
    db: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(limit, 200))
    logs = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": log.id,
            "category": log.category,
            "severity": log.severity,
            "action": log.action,
            "details": log.details,
            "created_at": log.created_at.isoformat(),
        }
        for log in logs
    ]
