"""Exchange API keys (one per Binance account: Demo Trading or Live) and the encrypted vault."""

from decimal import Decimal
from typing import Any
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_adapter_factory, get_db_session, get_strategy_runner, get_vault
from app.core.security import CredentialVault, mask_api_key
from app.domains.cex.adapters.factory import ExchangeAdapterFactory
from app.domains.cex.execution.runner import ContinuousStrategyRunner
from app.models.entities import ExchangeConnection, Position
from app.models.enums import ExchangeId, MarketType, PositionStatus, TradingEnvironment

router = APIRouter(prefix="/connections", tags=["Exchange Connections"])

DEFAULT_CAP_USD = Decimal("1000.0")
ENV_NAMES = {TradingEnvironment.DEMO: "Binance Demo Trading", TradingEnvironment.LIVE: "your real Binance account"}


class CreateConnectionRequest(BaseModel):
    exchange_id: str = "BINANCE"
    label: str = ""
    api_key: str
    api_secret: str
    # DEMO: Binance Demo Trading key (made on demo.binance.com). LIVE: real account key.
    environment: TradingEnvironment | None = None
    # Older clients: is_testnet=True means DEMO. Ignored when `environment` is given.
    is_testnet: bool = True
    spot_capital_cap_usd: float | None = None
    futures_capital_cap_usd: float | None = None


class ConnectionResponse(BaseModel):
    id: str
    exchange_id: str
    label: str
    api_key_masked: str
    environment: str
    is_testnet: bool
    is_active: bool
    # True for the key the engine trades with on its environment (the newest enabled one).
    in_use: bool
    spot_capital_cap_usd: float | None
    futures_capital_cap_usd: float | None
    created_at: str
    # Only on create: balances Binance returned while checking the key, and caveats.
    balances: dict[str, float] = {}
    warnings: list[str] = []


def _in_use_ids(conns: list[ExchangeConnection]) -> set[str]:
    """Newest enabled Binance key per environment: the one the engine uses."""
    chosen: dict[str, ExchangeConnection] = {}
    for c in conns:
        if not c.is_enabled or c.exchange_id != ExchangeId.BINANCE.value:
            continue
        best = chosen.get(c.environment)
        if best is None or c.created_at > best.created_at:
            chosen[c.environment] = c
    return {c.id for c in chosen.values()}


def _to_response(c: ExchangeConnection, in_use: bool, **extra: Any) -> ConnectionResponse:
    return ConnectionResponse(
        id=c.id,
        exchange_id=c.exchange_id,
        label=c.label,
        api_key_masked=c.api_key_masked,
        environment=c.environment,
        is_testnet=c.is_testnet,
        is_active=c.is_enabled,
        in_use=in_use,
        spot_capital_cap_usd=float(c.spot_capital_cap_usd) if c.spot_capital_cap_usd is not None else None,
        futures_capital_cap_usd=float(c.futures_capital_cap_usd) if c.futures_capital_cap_usd is not None else None,
        created_at=c.created_at.isoformat(),
        **extra,
    )


def _cap(value: float | None) -> Decimal:
    if value is None:
        return DEFAULT_CAP_USD
    if value < 0:
        raise HTTPException(status_code=400, detail="Capital caps cannot be negative (0 means no cap).")
    return Decimal(str(value))


@router.get("", response_model=list[ConnectionResponse])
async def list_connections(
    db: AsyncSession = Depends(get_db_session),
) -> list[ConnectionResponse]:
    conns = list((await db.execute(select(ExchangeConnection).order_by(ExchangeConnection.created_at.desc()))).scalars())
    in_use = _in_use_ids(conns)
    return [_to_response(c, c.id in in_use) for c in conns]


@router.post("", response_model=ConnectionResponse)
async def create_connection(
    req: CreateConnectionRequest,
    db: AsyncSession = Depends(get_db_session),
    vault: CredentialVault = Depends(get_vault),
    runner: ContinuousStrategyRunner = Depends(get_strategy_runner),
) -> ConnectionResponse:
    """Check a Binance API key with Binance, then store it encrypted.

    The key is refused when Binance rejects it on the chosen account; if it works on the
    other account instead, the message says so (a common mix-up between Demo and Live).
    """
    exch_str = req.exchange_id.upper().strip()
    if exch_str != ExchangeId.BINANCE.value:
        raise HTTPException(status_code=400, detail="Only Binance API keys are supported.")
    # Keys pasted from the browser often carry spaces or line breaks.
    api_key = "".join(req.api_key.split())
    api_secret = "".join(req.api_secret.split())
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="API key and secret are required.")
    env = req.environment or (TradingEnvironment.DEMO if req.is_testnet else TradingEnvironment.LIVE)
    spot_cap, futures_cap = _cap(req.spot_capital_cap_usd), _cap(req.futures_capital_cap_usd)

    check = await runner.verify_api_key(api_key, api_secret, env)
    if not check["ok"]:
        if check["belongs_to"]:
            other = TradingEnvironment(check["belongs_to"])
            raise HTTPException(
                status_code=400,
                detail=(
                    f"This key belongs to {ENV_NAMES[other]}, not {ENV_NAMES[env]}. "
                    f"Choose \"{'Live' if other == TradingEnvironment.LIVE else 'Demo Trading'}\" as the account."
                ),
            )
        if check["rejected"]:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{ENV_NAMES[env][0].upper()}{ENV_NAMES[env][1:]} rejected this key. Check the key and secret, "
                    "that it has Reading and Trading permissions, and any IP restriction on it."
                ),
            )
        raise HTTPException(
            status_code=502,
            detail="Could not reach Binance to check this key. Check your connection and try again.",
        )

    warnings: list[str] = []
    if MarketType.FUTURES_USDT.value not in check["balances"]:
        warnings.append("Futures are not enabled for this key; USDT-M futures strategies will not trade with it.")
    if MarketType.SPOT.value not in check["balances"]:
        warnings.append("Spot access failed for this key; spot strategies will not trade with it.")

    conn = ExchangeConnection(
        id=str(uuid.uuid4()),
        exchange_id=exch_str,
        label=req.label.strip() or ("Binance Demo" if env == TradingEnvironment.DEMO else "Binance Live"),
        api_key_masked=mask_api_key(api_key),
        api_key_encrypted=vault.encrypt(api_key, associated_data=exch_str),
        api_secret_encrypted=vault.encrypt(api_secret, associated_data=exch_str),
        environment=env.value,
        is_enabled=True,
        spot_capital_cap_usd=spot_cap,
        futures_capital_cap_usd=futures_cap,
    )
    db.add(conn)
    await db.commit()

    if env == runner.environment:
        try:
            await runner.refresh_account()  # header shows the new balance right away
        except Exception:
            pass

    return _to_response(
        conn,
        in_use=True,  # the newest key of its environment
        balances={k: float(v) for k, v in check["balances"].items()},
        warnings=warnings,
    )


@router.delete("/{connection_id}")
async def delete_connection(
    connection_id: str,
    db: AsyncSession = Depends(get_db_session),
    runner: ContinuousStrategyRunner = Depends(get_strategy_runner),
) -> dict[str, str]:
    conn = await db.get(ExchangeConnection, connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Exchange connection not found.")

    # Positions need their key to be closed and to have their stops checked.
    open_ids = (
        await db.execute(
            select(Position.id).where(
                Position.exchange_connection_id == connection_id,
                Position.status == PositionStatus.OPEN.value,
            )
        )
    ).scalars().all()
    if open_ids:
        raise HTTPException(
            status_code=409,
            detail=f"{len(open_ids)} open position(s) use this API key. Close them before removing it.",
        )

    await db.delete(conn)
    await db.commit()
    await runner.forget_connection(connection_id)
    try:
        await runner.refresh_account()
    except Exception:
        pass
    return {"status": "DELETED", "id": connection_id}


@router.get("/{connection_id}/balance")
async def get_connection_balance(
    connection_id: str,
    market_type: str = "FUTURES_USDT",
    db: AsyncSession = Depends(get_db_session),
    factory: ExchangeAdapterFactory = Depends(get_adapter_factory),
) -> dict[str, Any]:
    conn = await db.get(ExchangeConnection, connection_id)
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
