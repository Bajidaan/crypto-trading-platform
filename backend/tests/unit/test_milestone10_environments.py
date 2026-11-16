"""Milestone 10: DEMO (Binance Demo Trading) and LIVE environments replace paper trading."""

from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
import sqlite3
from typing import Any, AsyncIterator

import httpx
from sqlalchemy import select

from app.api.deps import AppState, set_app_state
from app.config import Settings
from app.core.database import MIGRATIONS_DIR, build_async_engine, build_session_factory, init_database
from app.domains.cex.adapters.binance import BinanceAdapter
from app.main import create_app
from app.models.entities import ExchangeConnection, GlobalRiskState, Position
from app.models.enums import MarketType, TradingEnvironment
from tests.unit.test_milestone8_live_execution import LiveMockAdapter, StubAdapterFactory


@asynccontextmanager
async def _client() -> AsyncIterator[tuple[httpx.AsyncClient, AppState, LiveMockAdapter]]:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        master_vault_secret="milestone10-test-master-key-32-bytes!",
        market_stream_enabled=False,
    )
    state = AppState(settings)
    await init_database(state.engine)
    adapter = LiveMockAdapter()
    # Never reach the real Binance from tests.
    state.strategy_runner.adapter_factory = StubAdapterFactory(adapter)  # type: ignore[assignment]
    set_app_state(state)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, state, adapter
    await state.engine.dispose()


async def _add_key(client: httpx.AsyncClient, environment: str, label: str) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/connections",
        json={"exchange_id": "BINANCE", "label": label, "api_key": "k" * 20, "api_secret": "s" * 20,
              "environment": environment},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_demo_keys_use_binance_demo_trading_hosts() -> None:
    demo = BinanceAdapter(api_key="k", api_secret="s", is_testnet=True)
    live = BinanceAdapter(api_key="k", api_secret="s", is_testnet=False)
    assert demo._base_url(MarketType.SPOT) == "https://demo-api.binance.com"
    assert demo._base_url(MarketType.FUTURES_USDT) == "https://demo-fapi.binance.com"
    assert live._base_url(MarketType.SPOT) == "https://api.binance.com"
    assert live._base_url(MarketType.FUTURES_USDT) == "https://fapi.binance.com"


async def test_status_starts_on_demo_without_a_key() -> None:
    async with _client() as (client, _, _):
        data = (await client.get("/api/v1/system/status")).json()
    assert data["environment"] == "DEMO"
    assert data["connection_label"] is None
    assert data["equity_usd"] is None
    assert data["open_positions"] == 0


async def test_switch_to_live_needs_a_key_and_confirmation() -> None:
    async with _client() as (client, state, _):
        no_key = await client.post("/api/v1/system/environment", json={"environment": "LIVE", "confirm_live": True})
        assert no_key.status_code == 400 and "No enabled LIVE" in no_key.json()["detail"]

        key = await _add_key(client, "LIVE", "Real account")
        assert key["environment"] == "LIVE" and key["is_testnet"] is False

        unconfirmed = await client.post("/api/v1/system/environment", json={"environment": "LIVE"})
        assert unconfirmed.status_code == 400 and "confirm_live" in unconfirmed.json()["detail"]

        ok = await client.post("/api/v1/system/environment", json={"environment": "LIVE", "confirm_live": True})
        assert ok.status_code == 200
        assert ok.json() == {"environment": "LIVE", "previous": "DEMO", "connection_label": "Real account"}

        status = (await client.get("/api/v1/system/status")).json()
        assert status["environment"] == "LIVE"
        assert status["connection_label"] == "Real account"
        assert status["equity_usd"] == 10000.0  # from the account's futures balance

        async with state.session_factory() as s:
            assert (await s.get(GlobalRiskState, 1)).active_environment == "LIVE"  # survives restarts

        back = await client.post("/api/v1/system/environment", json={"environment": "DEMO"})
        assert back.status_code == 400  # no DEMO key yet
        await _add_key(client, "DEMO", "Demo trading")
        back = await client.post("/api/v1/system/environment", json={"environment": "DEMO"})
        assert back.status_code == 200 and back.json()["environment"] == "DEMO"


async def test_positions_and_orders_follow_the_environment() -> None:
    async with _client() as (client, state, _):
        demo = await _add_key(client, "DEMO", "Demo")
        async with state.session_factory() as s:
            for pid, mode in (("p-demo", "DEMO"), ("p-live", "LIVE"), ("p-paper", "PAPER")):
                s.add(Position(id=pid, execution_mode=mode, symbol="BTC/USDT", market_type="FUTURES_USDT",
                               side="LONG", entry_price=Decimal("60000"), initial_qty=Decimal("0.1"),
                               current_qty=Decimal("0.1"), status="OPEN",
                               exchange_connection_id=demo["id"] if mode == "DEMO" else None))
            await s.commit()
        state.strategy_runner._last_price[("BTC/USDT", MarketType.FUTURES_USDT)] = Decimal("61000")

        demo_rows = (await client.get("/api/v1/positions")).json()
        live_rows = (await client.get("/api/v1/positions", params={"environment": "LIVE"})).json()
        status = (await client.get("/api/v1/system/status")).json()

    assert [r["id"] for r in demo_rows] == ["p-demo"]  # paper history is not shown
    assert demo_rows[0]["current_price"] == 61000.0
    assert abs(demo_rows[0]["unrealized_pnl_usd"] - 100.0) < 1e-9  # (61000 - 60000) * 0.1
    assert [r["id"] for r in live_rows] == ["p-live"]
    assert status["open_positions"] == 1
    assert status["other_environment_open_positions"] == 1


async def test_key_with_open_positions_cannot_be_deleted() -> None:
    async with _client() as (client, state, _):
        key = await _add_key(client, "DEMO", "Demo")
        async with state.session_factory() as s:
            s.add(Position(id="p1", execution_mode="DEMO", symbol="BTC/USDT", market_type="FUTURES_USDT",
                           side="LONG", entry_price=Decimal("1"), initial_qty=Decimal("1"),
                           current_qty=Decimal("1"), status="OPEN", exchange_connection_id=key["id"]))
            await s.commit()
        blocked = await client.delete(f"/api/v1/connections/{key['id']}")
        assert blocked.status_code == 409

        async with state.session_factory() as s:
            (await s.get(Position, "p1")).status = "CLOSED"
            await s.commit()
        assert (await client.delete(f"/api/v1/connections/{key['id']}")).status_code == 200


async def test_deleting_a_key_keeps_its_strategies() -> None:
    from app.models.entities import StrategyConfig

    async with _client() as (client, state, _):
        key = await _add_key(client, "DEMO", "Demo")
        async with state.session_factory() as s:
            s.add(StrategyConfig(id="s1", name="Trend", strategy_class="ema_rsi_atr",
                                 exchange_connection_id=key["id"]))
            await s.commit()
        assert (await client.delete(f"/api/v1/connections/{key['id']}")).status_code == 200
        async with state.session_factory() as s:
            strat = await s.get(StrategyConfig, "s1")
    assert strat is not None and strat.exchange_connection_id is None


async def test_migration_maps_testnet_keys_to_demo(tmp_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "pre_env.db"
    engine = build_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")

    def _to_0003(sync_conn: Any) -> None:
        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        cfg.attributes["connection"] = sync_conn
        command.upgrade(cfg, "0003")

    async with engine.connect() as conn:
        await conn.run_sync(_to_0003)
        await conn.commit()
    await engine.dispose()

    raw = sqlite3.connect(db_file)
    for cid, testnet in (("c-test", 1), ("c-real", 0)):
        raw.execute(
            "insert into exchange_connections (id, exchange_id, label, api_key_masked, api_key_encrypted,"
            " api_secret_encrypted, is_testnet, spot_capital_cap_usd, futures_capital_cap_usd, is_enabled,"
            " created_at, updated_at) values (?, 'BINANCE', ?, 'x', 'x', 'y', ?, '1000', '1000', 1,"
            " '2026-01-01', '2026-01-01')",
            (cid, cid, testnet),
        )
    raw.commit()
    raw.close()

    engine = build_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    await init_database(engine)
    async with build_session_factory(engine)() as s:
        envs = {c.id: c.environment for c in (await s.execute(select(ExchangeConnection))).scalars()}
        risk = await s.get(GlobalRiskState, 1)
    await engine.dispose()
    assert envs == {"c-test": "DEMO", "c-real": "LIVE"}
    assert risk.active_environment == TradingEnvironment.DEMO.value
