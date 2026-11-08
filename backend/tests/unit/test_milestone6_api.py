"""Milestone 6 Test Suite: FastAPI REST Endpoints & WebSocket Gateway Integration."""

import json
from contextlib import asynccontextmanager
from decimal import Decimal
import httpx
import pytest

from app.api.deps import AppState, set_app_state
from app.config import Settings
from app.core.database import init_database
from app.main import create_app


@asynccontextmanager
async def create_test_client():
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        master_vault_secret="milestone6-test-master-key-32-bytes!!",
        webhook_hmac_secret="test-webhook-secret-key-2026",
    )
    app_state = AppState(settings)
    await init_database(app_state.engine)
    set_app_state(app_state)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    await app_state.engine.dispose()


def test_root_and_system_status_endpoints() -> None:
    async def _test() -> None:
        async with create_test_client() as client:
            # 1. Root
            resp = await client.get("/")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ONLINE"

            # 2. System Status
            status_resp = await client.get("/api/v1/system/status")
            assert status_resp.status_code == 200
            data = status_resp.json()
            assert data["is_paused"] is False
            assert data["kill_switch_active"] is False
            assert data["max_concurrent_positions"] == 5

    import asyncio
    asyncio.run(_test())


def test_system_circuit_breaker_pause_resume_and_kill_switch() -> None:
    async def _test() -> None:
        async with create_test_client() as client:
            # 1. Pause
            p_resp = await client.post("/api/v1/system/pause")
            assert p_resp.status_code == 200
            assert p_resp.json()["status"] == "PAUSED"

            # Status reflects paused
            status_1 = (await client.get("/api/v1/system/status")).json()
            assert status_1["is_paused"] is True

            # 2. Resume
            r_resp = await client.post("/api/v1/system/resume")
            assert r_resp.status_code == 200
            assert r_resp.json()["status"] == "ACTIVE"

            # 3. Kill Switch
            k_resp = await client.post("/api/v1/system/kill-switch")
            assert k_resp.status_code == 200
            assert k_resp.json()["status"] == "KILL_SWITCH_ACTIVE"

            # Trying to resume while kill switch is active must be rejected with HTTP 400
            r_blocked = await client.post("/api/v1/system/resume")
            assert r_blocked.status_code == 400

    import asyncio
    asyncio.run(_test())


def test_exchange_connections_crud_and_masking() -> None:
    async def _test() -> None:
        async with create_test_client() as client:
            # 1. Create connection
            payload = {
                "exchange_id": "BINANCE",
                "label": "Binance Main Futures",
                "api_key": "BINANCE_REAL_KEY_123456789",
                "api_secret": "BINANCE_REAL_SECRET_ABCDEFGH",
                "is_testnet": True,
                "futures_capital_cap_usd": 5000.0,
            }
            create_resp = await client.post("/api/v1/connections", json=payload)
            assert create_resp.status_code == 200
            conn_data = create_resp.json()
            assert conn_data["api_key_masked"] == "BINA****6789"
            conn_id = conn_data["id"]

            # 2. List connections
            list_resp = await client.get("/api/v1/connections")
            assert list_resp.status_code == 200
            items = list_resp.json()
            assert len(items) == 1
            assert items[0]["id"] == conn_id

            # 3. Delete connection
            del_resp = await client.delete(f"/api/v1/connections/{conn_id}")
            assert del_resp.status_code == 200
            assert del_resp.json()["status"] == "DELETED"

    import asyncio
    asyncio.run(_test())


def test_strategy_management_and_webhook_signal() -> None:
    async def _test() -> None:
        async with create_test_client() as client:
            # 1. Available classes
            avail = await client.get("/api/v1/strategies/available-classes")
            assert avail.status_code == 200
            assert "ema_rsi_atr" in avail.json()

            # 2. Create strategy
            strat_payload = {
                "name": "Live BTC 15m Trend",
                "strategy_class": "ema_rsi_atr",
                "market_type": "FUTURES_USDT",
                "symbols": ["BTC/USDT"],
                "timeframe": "15m",
                "parameters": {"fast_ema_period": 9, "slow_ema_period": 21},
            }
            create_resp = await client.post("/api/v1/strategies", json=strat_payload)
            assert create_resp.status_code == 200
            strat_id = create_resp.json()["id"]

            # 3. Toggle strategy active
            patch_resp = await client.patch(
                f"/api/v1/strategies/{strat_id}", json={"is_active": False}
            )
            assert patch_resp.status_code == 200
            assert patch_resp.json()["is_active"] is False

            # 4. Ingest external webhook signal
            webhook_payload = {
                "symbol": "BTC/USDT",
                "action": "BUY",
                "market_type": "FUTURES_USDT",
                "signal_price": 65000.0,
                "stop_loss": 63700.0,
                "take_profit_1": 66950.0,
            }
            wh_resp = await client.post("/api/v1/webhooks/signal", json=webhook_payload)
            assert wh_resp.status_code == 200
            assert wh_resp.json()["status"] == "ACCEPTED"

    import asyncio
    asyncio.run(_test())
