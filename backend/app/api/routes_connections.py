"""Exchange API Connections and Encrypted Credential Vault Management."""

from decimal import Decimal
from typing import Any
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_adapter_factory, get_db_session, get_vault
from app.core.security import CredentialVault, mask_api_key
from app.domains.cex.adapters.factory import ExchangeAdapterFactory
from app.models.entities import ExchangeConnection
from app.models.enums import ExchangeId, MarketType

router = APIRouter(prefix="/connections", tags=["Exchange Connections"])


class CreateConnectionRequest(BaseModel):
    exchange_id: str  # "BINANCE" or "BYBIT"
    label: str
    api_key: str
    api_secret: str
    is_testnet: bool = True
    spot_capital_cap_usd: float | None = None
    futures_capital_cap_usd: float | None = None


class ConnectionResponse(BaseModel):
    id: str
    exchange_id: str
    label: str
    api_key_masked: str
    is_testnet: bool
    is_active: bool
    spot_capital_cap_usd: float | None
    futures_capital_cap_usd: float | None
    created_at: str


@router.get("", response_model=list[ConnectionResponse])
async def list_connections(
    db: AsyncSession = Depends(get_db_session),
) -> list[ConnectionResponse]:
    stmt = select(ExchangeConnection).order_by(ExchangeConnection.created_at.desc())
    conns = (await db.execute(stmt)).scalars().all()
    return [
        ConnectionResponse(
            id=c.id,
            exchange_id=c.exchange_id,
            label=c.label,
            api_key_masked=c.api_key_masked,
            is_testnet=c.is_testnet,
            is_active=c.is_enabled,
            spot_capital_cap_usd=float(c.spot_capital_cap_usd) if c.spot_capital_cap_usd else None,
            futures_capital_cap_usd=float(c.futures_capital_cap_usd) if c.futures_capital_cap_usd else None,
            created_at=c.created_at.isoformat(),
        )
        for c in conns
    ]


@router.post("", response_model=ConnectionResponse)
async def create_connection(
    req: CreateConnectionRequest,
    db: AsyncSession = Depends(get_db_session),
    vault: CredentialVault = Depends(get_vault),
) -> ConnectionResponse:
    exch_str = req.exchange_id.upper().strip()
    if exch_str not in ("BINANCE", "BYBIT"):
        raise HTTPException(status_code=400, detail="Only 'BINANCE' and 'BYBIT' are supported in Phase 1.")

    conn_id = str(uuid.uuid4())
    masked_key = mask_api_key(req.api_key)
    encrypted_key = vault.encrypt(req.api_key, associated_data=exch_str)
    encrypted_secret = vault.encrypt(req.api_secret, associated_data=exch_str)

    conn = ExchangeConnection(
        id=conn_id,
        exchange_id=exch_str,
        label=req.label,
        api_key_masked=masked_key,
        api_key_encrypted=encrypted_key,
        api_secret_encrypted=encrypted_secret,
        is_testnet=req.is_testnet,
        is_enabled=True,
        spot_capital_cap_usd=Decimal(str(req.spot_capital_cap_usd)) if req.spot_capital_cap_usd is not None else Decimal("1000.0"),
        futures_capital_cap_usd=Decimal(str(req.futures_capital_cap_usd)) if req.futures_capital_cap_usd is not None else Decimal("1000.0"),
    )
    session_added = session = db
    session_added.add(conn)
    await session_added.commit()

    return ConnectionResponse(
        id=conn.id,
        exchange_id=conn.exchange_id,
        label=conn.label,
        api_key_masked=conn.api_key_masked,
        is_testnet=conn.is_testnet,
        is_active=conn.is_enabled,
        spot_capital_cap_usd=float(conn.spot_capital_cap_usd) if conn.spot_capital_cap_usd else None,
        futures_capital_cap_usd=float(conn.futures_capital_cap_usd) if conn.futures_capital_cap_usd else None,
        created_at=conn.created_at.isoformat(),
    )


@router.delete("/{connection_id}")
async def delete_connection(
    connection_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    stmt = select(ExchangeConnection).where(ExchangeConnection.id == connection_id)
    conn = (await db.execute(stmt)).scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Exchange connection not found.")

    await db.delete(conn)
    await db.commit()
    return {"status": "DELETED", "id": connection_id}


@router.get("/{connection_id}/balance")
async def get_connection_balance(
    connection_id: str,
    market_type: str = "FUTURES_USDT",
    db: AsyncSession = Depends(get_db_session),
    factory: ExchangeAdapterFactory = Depends(get_adapter_factory),
) -> dict[str, Any]:
    stmt = select(ExchangeConnection).where(ExchangeConnection.id == connection_id)
    conn = (await db.execute(stmt)).scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Exchange connection not found.")

    adapter = factory.create_from_connection(conn)
    m_type = MarketType(market_type.upper().strip())
    try:
        bal = await adapter.fetch_balance(market_type=m_type)
        return {
            "exchange_id": bal.exchange_id.value,
            "market_type": bal.market_type.value,
            "currency": bal.currency,
            "total_equity": float(bal.total_equity),
            "available_balance": float(bal.available_balance),
            "used_margin": float(bal.used_margin),
            "unrealized_pnl": float(bal.unrealized_pnl),
        }
    finally:
        await adapter.close()
