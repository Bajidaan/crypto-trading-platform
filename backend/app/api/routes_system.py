"""System Management, Circuit Breakers, Kill Switch, and Audit Trail API."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, get_event_bus, get_paper_broker, get_risk_gatekeeper, get_stream_manager
from app.core.events import EventBus, RiskAlertEvent
from app.domains.cex.execution.paper_broker import PaperTradingBroker
from app.domains.cex.market_data.stream_manager import MarketStreamManager
from app.domains.cex.risk.gatekeeper import RiskGatekeeper
from app.models.entities import AuditLog, GlobalRiskState

router = APIRouter(prefix="/system", tags=["System"])


class SystemStatusResponse(BaseModel):
    is_paused: bool
    kill_switch_active: bool
    max_concurrent_positions: int
    max_daily_drawdown_pct: float
    max_leverage_cap: int
    open_paper_positions: int
    total_paper_equity: float
    active_stream_subscriptions: int
    event_bus_published_count: int


@router.get("/status", response_model=SystemStatusResponse)
async def get_system_status(
    db: AsyncSession = Depends(get_db_session),
    gatekeeper: RiskGatekeeper = Depends(get_risk_gatekeeper),
    paper_broker: PaperTradingBroker = Depends(get_paper_broker),
    stream_manager: MarketStreamManager = Depends(get_stream_manager),
    event_bus: EventBus = Depends(get_event_bus),
) -> SystemStatusResponse:
    risk_state = (
        await db.execute(select(GlobalRiskState).where(GlobalRiskState.id == 1))
    ).scalar_one_or_none()

    if risk_state:
        gatekeeper.update_risk_state(risk_state)
    state = gatekeeper.risk_state

    from app.models.entities import Position
    stmt_open = select(func.count(Position.id)).where(Position.status == "OPEN")
    db_open_count = (await db.execute(stmt_open)).scalar() or 0
    actual_open_count = max(db_open_count, paper_broker.open_positions_count)

    return SystemStatusResponse(
        is_paused=state.is_paused,
        kill_switch_active=state.kill_switch_active,
        max_concurrent_positions=state.max_concurrent_positions,
        max_daily_drawdown_pct=float(state.max_daily_drawdown_pct),
        max_leverage_cap=state.max_leverage_cap,
        open_paper_positions=actual_open_count,
        total_paper_equity=float(paper_broker.total_equity),
        active_stream_subscriptions=len(stream_manager.subscriptions),
        event_bus_published_count=event_bus.stats["published_count"],
    )


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
