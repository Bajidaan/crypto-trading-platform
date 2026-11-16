"""Milestone 9: exact decimals, migrations, unique keys, batched marks and chart serving."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from typing import Any
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.database import (
    MIGRATIONS_DIR,
    build_async_engine,
    build_session_factory,
    init_database,
)
from app.core.events import EventBus, TradeSignalEvent
from app.domains.cex.adapters.base import CandleBar
from app.domains.cex.execution import HybridSlTpManager, LiveExecutionBroker
from app.domains.cex.execution.runner import ContinuousStrategyRunner
from app.domains.cex.risk import RiskGatekeeper
from app.models.entities import ExchangeConnection, Fill, Order, Position, StrategyConfig
from app.models.enums import MarketType, PositionSide, SignalSource
from tests.unit.test_milestone8_live_execution import LiveMockAdapter, StubAdapterFactory


async def _db() -> Any:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    return engine, build_session_factory(engine)


def _position(pid: str, **kw: Any) -> Position:
    base = dict(
        id=pid,
        execution_mode="PAPER",
        symbol="BTC/USDT",
        market_type="FUTURES_USDT",
        side="LONG",
        entry_price=Decimal("64999.123456789012"),
        initial_qty=Decimal("0.123456789012"),
        current_qty=Decimal("0.123456789012"),
        status="OPEN",
    )
    base.update(kw)
    return Position(**base)


async def test_decimals_round_trip_exactly() -> None:
    engine, sf = await _db()
    async with sf() as s:
        s.add(_position("p1", realized_pnl_usd=Decimal("0.1") + Decimal("0.2")))
        await s.commit()
    async with sf() as s:
        p = (await s.execute(select(Position))).scalar_one()
        stored = (await s.execute(text("select typeof(entry_price) from positions"))).scalar_one()
    assert p.entry_price == Decimal("64999.123456789012")
    assert p.current_qty == Decimal("0.123456789012")
    assert p.realized_pnl_usd == Decimal("0.3")
    assert stored == "text"
    await engine.dispose()


async def test_same_trade_id_allowed_on_different_orders() -> None:
    engine, sf = await _db()
    async with sf() as s:
        s.add(_position("p1"))
        for oid, cid in (("o1", "cid-1"), ("o2", "cid-2")):
            s.add(Order(id=oid, position_id="p1", client_order_id=cid, symbol="BTC/USDT", market_type="SPOT",
                        side="BUY", requested_qty=Decimal("1")))
        # BTC and ETH fills can carry the same Binance trade id.
        s.add(Fill(order_id="o1", exchange_trade_id="123", fill_price=Decimal("1"), fill_qty=Decimal("1")))
        s.add(Fill(order_id="o2", exchange_trade_id="123", fill_price=Decimal("1"), fill_qty=Decimal("1")))
        await s.commit()
    async with sf() as s:
        s.add(Fill(order_id="o1", exchange_trade_id="123", fill_price=Decimal("1"), fill_qty=Decimal("1")))
        with pytest.raises(IntegrityError):
            await s.commit()
    await engine.dispose()


async def _runner(sf: Any, adapter: LiveMockAdapter | None = None) -> ContinuousStrategyRunner:
    bus = EventBus()
    sl_tp = HybridSlTpManager(event_bus=bus)
    adapter = adapter or LiveMockAdapter()
    async with sf() as s:
        s.add(ExchangeConnection(id="demo", exchange_id="BINANCE", label="demo", api_key_masked="d***",
                                 api_key_encrypted="x", api_secret_encrypted="y", environment="DEMO"))
        await s.commit()
    return ContinuousStrategyRunner(
        session_factory=sf,
        event_bus=bus,
        risk_gatekeeper=RiskGatekeeper(event_bus=bus),
        adapter_factory=StubAdapterFactory(adapter),  # type: ignore[arg-type]
        live_broker=LiveExecutionBroker(session_factory=sf, event_bus=bus, sl_tp_manager=sl_tp),
        sl_tp_manager=sl_tp,
        enable_kline_stream=False,
        market_data_adapter=adapter,
    )


async def test_orders_in_same_second_are_all_saved() -> None:
    """Two symbols of one strategy filling in the same second both keep their order records."""
    engine, sf = await _db()
    async with sf() as s:
        cfg = StrategyConfig(id="s1", name="multi", strategy_class="x",
                             market_type="FUTURES_USDT", symbols=["BTC/USDT", "ETH/USDT"], is_active=True)
        s.add(cfg)
        await s.commit()
        await s.refresh(cfg)
    runner = await _runner(sf)
    for sym, price in (("BTC/USDT", Decimal("65000")), ("ETH/USDT", Decimal("3000"))):
        sig = TradeSignalEvent(strategy_id="s1", source=SignalSource.STRATEGY, symbol=sym, side=PositionSide.LONG,
                               target_weight=Decimal("0.1"), stop_loss_price=price * Decimal("0.95"))
        await runner._process_signal(sig, cfg, price)
    async with sf() as s:
        orders = (await s.execute(select(Order))).scalars().all()
    entries = sorted(o.symbol for o in orders if o.purpose == "ENTRY")
    assert entries == ["BTC/USDT", "ETH/USDT"]
    await engine.dispose()


async def test_legacy_database_is_stamped_and_migrated(tmp_path: Path) -> None:
    """A database made by the old create_all() code keeps every row through the migration."""
    db_file = tmp_path / "legacy.db"
    # Build the pre-migration schema exactly as the old code did, with REAL-stored decimals.
    from alembic import command
    from alembic.config import Config

    engine = build_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")

    def _baseline_only(sync_conn: Any) -> None:
        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        cfg.attributes["connection"] = sync_conn
        command.upgrade(cfg, "0001")

    async with engine.connect() as conn:
        await conn.run_sync(_baseline_only)
        await conn.commit()
    await engine.dispose()

    raw = sqlite3.connect(db_file)
    raw.execute("drop table alembic_version")
    raw.execute(
        "insert into positions (id, execution_mode, symbol, market_type, side, margin_mode, leverage, entry_price,"
        " initial_qty, current_qty, stop_order_live, take_profit_state, realized_pnl_usd, unrealized_pnl_usd,"
        " total_fees_usd, status, opened_at, updated_at) values ('p1','PAPER','BTC/USDT','SPOT','LONG','ISOLATED',1,"
        " 65000.5, 0.01, 0.01, 0, '[]', -1.25, 0, 0.5, 'CLOSED', '2026-01-01', '2026-01-01')"
    )
    raw.execute(
        "insert into orders (id, position_id, client_order_id, symbol, market_type, purpose, order_type, side,"
        " reduce_only, requested_qty, filled_qty, fee_usd, status, created_at, updated_at) values ('o1','p1','c1',"
        " 'BTC/USDT','SPOT','ENTRY','MARKET','BUY',0,0.01,0.01,0.5,'FILLED','2026-01-01','2026-01-01')"
    )
    raw.commit()
    raw.close()

    engine = build_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    await init_database(engine)
    sf = build_session_factory(engine)
    async with sf() as s:
        pos = (await s.execute(select(Position))).scalar_one()
        orders = (await s.execute(select(Order))).scalars().all()
        version = (await s.execute(text("select version_num from alembic_version"))).scalar_one()
    await engine.dispose()

    assert version == "0004"
    assert pos.entry_price == Decimal("65000.5") and pos.realized_pnl_usd == Decimal("-1.25")
    assert len(orders) == 1  # rebuilding positions must not cascade-delete its orders


async def test_chart_served_from_memory_after_first_load() -> None:
    engine, sf = await _db()
    adapter = LiveMockAdapter()
    calls: list[int] = []
    base = datetime(2026, 11, 16, tzinfo=timezone.utc)

    async def fetch_ohlcv(symbol: str, market_type: MarketType, timeframe: str, since_ms: int | None = None, limit: int = 500) -> list[CandleBar]:
        calls.append(limit)
        return [
            CandleBar(symbol=symbol, market_type=market_type, timeframe=timeframe,
                      open_time=base + timedelta(minutes=15 * i), open=Decimal("1"), high=Decimal("2"),
                      low=Decimal("0.5"), close=Decimal("1.5"), volume=Decimal("1"))
            for i in range(limit)
        ]

    adapter.fetch_ohlcv = fetch_ohlcv  # type: ignore[method-assign]
    runner = await _runner(sf, adapter)

    first = await runner.chart_candles("BTC/USDT", "15m", MarketType.FUTURES_USDT, 150)
    second = await runner.chart_candles("BTC/USDT", "15m", MarketType.FUTURES_USDT, 150)
    assert len(first) == len(second) == 150
    assert calls == [150]  # history once; the repeat poll is answered from the buffer
    assert ("BTC/USDT", "15m", MarketType.FUTURES_USDT) in runner._chart_watch
    await engine.dispose()
