"""Milestone 5 Test Suite: Risk Gatekeeper, Position Sizer, Paper/Live Brokers, Hybrid SL/TP, & Reconciler."""

from datetime import datetime, timezone
from decimal import Decimal
import uuid
import pytest
from sqlalchemy import select

from app.core.database import build_async_engine, build_session_factory, init_database
from app.core.events import (
    DomainEvent,
    EventBus,
    OrderLifecycleEvent,
    PositionUpdatedEvent,
    RiskAlertEvent,
    TakeProfitTargetSpec,
    TradeSignalEvent,
)
from app.domains.cex.adapters.base import (
    AccountBalance,
    CandleBar,
    ExchangePositionSnapshot,
    IExchangeAdapter,
    MarketRules,
    NormalizedOrderRequest,
    NormalizedOrderResponse,
    OrderNotFoundError,
    TickerSnapshot,
)
from app.domains.cex.execution import (
    HybridSlTpManager,
    LiveExecutionBroker,
    PaperTradingBroker,
    StartupReconciler,
)
from app.domains.cex.risk import PositionSizer, RiskGatekeeper
from app.models.entities import GlobalRiskState, Order, Position, StrategyConfig
from app.models.enums import (
    ExchangeId,
    ExecutionMode,
    MarginMode,
    MarketType,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    SignalSource,
)


# =====================================================================
# 1. Mock Exchange Adapter for Execution Testing
# =====================================================================


class ExecutionMockAdapter(IExchangeAdapter):
    """Mock exchange adapter for simulating order placement, stops, and reconciliation."""

    def __init__(self, exchange_id: ExchangeId = ExchangeId.BINANCE) -> None:
        self._exchange_id = exchange_id
        self.placed_orders: list[NormalizedOrderRequest] = []
        self.placed_stops: list[dict[str, Any]] = []
        self.amended_stops: list[dict[str, Any]] = []
        self.open_orders_to_return: list[NormalizedOrderResponse] = []
        self.positions_to_return: list[ExchangePositionSnapshot] = []

    @property
    def exchange_id(self) -> ExchangeId:
        return self._exchange_id

    @property
    def is_testnet(self) -> bool:
        return True

    async def place_order(self, request: NormalizedOrderRequest) -> NormalizedOrderResponse:
        self.placed_orders.append(request)
        return NormalizedOrderResponse(
            client_order_id=request.client_order_id,
            exchange_order_id=f"mock-ord-{len(self.placed_orders)}",
            symbol=request.symbol,
            market_type=request.market_type,
            side=request.side,
            order_type=request.order_type,
            status=OrderStatus.FILLED,
            requested_qty=request.quantity,
            filled_qty=request.quantity,
            avg_fill_price=Decimal("65000.00"),
            fee_usd=Decimal("1.95"),
        )

    async def place_hard_stop_loss(
        self,
        symbol: str,
        market_type: MarketType,
        side: OrderSide,
        quantity: Decimal,
        stop_price: Decimal,
        client_order_id: str,
        limit_price: Decimal | None = None,
    ) -> NormalizedOrderResponse:
        record = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "stop_price": stop_price,
            "client_order_id": client_order_id,
        }
        self.placed_stops.append(record)
        return NormalizedOrderResponse(
            client_order_id=client_order_id,
            exchange_order_id=f"mock-stop-{len(self.placed_stops)}",
            symbol=symbol,
            market_type=market_type,
            side=side,
            order_type=OrderType.STOP_MARKET,
            status=OrderStatus.OPEN,
            requested_qty=quantity,
            stop_price=stop_price,
            reduce_only=True,
        )

    async def amend_or_replace_stop_loss(
        self,
        existing_order_id: str,
        existing_client_order_id: str,
        symbol: str,
        market_type: MarketType,
        side: OrderSide,
        quantity: Decimal,
        new_stop_price: Decimal,
        new_client_order_id: str,
    ) -> NormalizedOrderResponse:
        record = {
            "existing_order_id": existing_order_id,
            "new_stop_price": new_stop_price,
            "new_client_order_id": new_client_order_id,
        }
        self.amended_stops.append(record)
        return NormalizedOrderResponse(
            client_order_id=new_client_order_id,
            exchange_order_id=existing_order_id,
            symbol=symbol,
            market_type=market_type,
            side=side,
            order_type=OrderType.STOP_MARKET,
            status=OrderStatus.OPEN,
            requested_qty=quantity,
            stop_price=new_stop_price,
            reduce_only=True,
        )

    async def fetch_open_orders(
        self, symbol: str | None, market_type: MarketType
    ) -> list[NormalizedOrderResponse]:
        return list(self.open_orders_to_return)

    async def fetch_open_positions(
        self, market_type: MarketType, symbol: str | None = None
    ) -> list[ExchangePositionSnapshot]:
        return list(self.positions_to_return)

    async def fetch_order(
        self, symbol: str, market_type: MarketType, exchange_order_id: str | None = None, client_order_id: str | None = None
    ) -> NormalizedOrderResponse:
        raise OrderNotFoundError("Order not found")

    # Stubs
    async def load_markets(self, market_type: MarketType, force_refresh: bool = False) -> dict[str, MarketRules]:
        return {}

    async def get_market_rules(self, symbol: str, market_type: MarketType) -> MarketRules:
        raise NotImplementedError

    async def fetch_balance(self, market_type: MarketType) -> AccountBalance:
        raise NotImplementedError

    async def fetch_ticker(self, symbol: str, market_type: MarketType) -> TickerSnapshot:
        raise NotImplementedError

    async def fetch_ohlcv(self, symbol: str, market_type: MarketType, timeframe: str, since_ms: int | None = None, limit: int = 500) -> list[CandleBar]:
        return []

    async def configure_futures_symbol(self, symbol: str, leverage: int = 3, margin_mode: MarginMode = MarginMode.ISOLATED, one_way_mode: bool = True) -> None:
        pass

    async def cancel_order(self, exchange_order_id: str, symbol: str, market_type: MarketType, client_order_id: str | None = None) -> bool:
        return True

    async def cancel_all_orders(self, symbol: str, market_type: MarketType) -> int:
        return 0

    async def close_position_market(self, symbol: str, market_type: MarketType, side: PositionSide, quantity: Decimal, client_order_id: str) -> NormalizedOrderResponse:
        raise NotImplementedError

    async def close(self) -> None:
        pass


# =====================================================================
# 2. Risk Gatekeeper Tests
# =====================================================================


async def test_risk_gatekeeper_circuit_breakers() -> None:
    bus = EventBus()
    alerts: list[RiskAlertEvent] = []

    async def on_alert(ev: DomainEvent) -> None:
        if isinstance(ev, RiskAlertEvent):
            alerts.append(ev)

    bus.subscribe("risk:alert", on_alert)

    risk_state = GlobalRiskState(
        id=1,
        is_paused=False,
        kill_switch_active=False,
        max_concurrent_positions=2,
        max_daily_drawdown_pct=Decimal("5.0"),
        max_leverage_cap=10,
    )
    gatekeeper = RiskGatekeeper(event_bus=bus, risk_state=risk_state)

    valid_signal = TradeSignalEvent(
        strategy_id="s1",
        source=SignalSource.STRATEGY,
        symbol="BTC/USDT",
        market_type=MarketType.FUTURES_USDT,
        side=PositionSide.LONG,
        signal_price=Decimal("65000"),
        stop_loss_price=Decimal("63700"),
        leverage=3,
    )

    # 1. Normal state: Approved
    ok, err = await gatekeeper.evaluate_signal(
        signal=valid_signal,
        open_positions_count=1,
        current_equity=Decimal("10000"),
        daily_starting_equity=Decimal("10000"),
    )
    assert ok is True
    assert err is None

    # 2. Max Concurrent Positions reached (2/2): Rejected
    ok, err = await gatekeeper.evaluate_signal(
        signal=valid_signal,
        open_positions_count=2,
        current_equity=Decimal("10000"),
        daily_starting_equity=Decimal("10000"),
    )
    assert ok is False
    assert "positions limit reached" in (err or "")

    # 3. Daily Drawdown Breaker (starting 10k, current 9.4k -> 6% loss > 5% max): Rejected + Auto-Pause
    ok, err = await gatekeeper.evaluate_signal(
        signal=valid_signal,
        open_positions_count=0,
        current_equity=Decimal("9400"),
        daily_starting_equity=Decimal("10000"),
    )
    assert ok is False
    assert "drawdown circuit breaker" in (err or "")
    assert gatekeeper.risk_state.is_paused is True
    assert len(alerts) >= 1
    assert alerts[-1].rule_name == "MAX_DAILY_DRAWDOWN"

    # 4. Kill Switch active: Rejected immediately
    risk_state.kill_switch_active = True
    ok, err = await gatekeeper.evaluate_signal(
        signal=valid_signal,
        open_positions_count=0,
        current_equity=Decimal("10000"),
        daily_starting_equity=Decimal("10000"),
    )
    assert ok is False
    assert "Kill Switch is ACTIVE" in (err or "")


# =====================================================================
# 3. Position Sizer Tests
# =====================================================================


def test_position_sizer_math_and_filters() -> None:
    rules = MarketRules(
        symbol="BTC/USDT",
        exchange_symbol="BTCUSDT",
        market_type=MarketType.FUTURES_USDT,
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.10"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("10.0"),
        max_leverage=10,
    )

    # 1. Normal sizing: Equity $10,000, 1.0% risk ($100), Entry 50,000, SL 48,000 (distance = 2,000)
    # Raw quantity = 100 / 2,000 = 0.05 BTC
    qty, err = PositionSizer.calculate_quantity(
        equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("1.0"),
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("48000"),
        leverage=3,
        market_rules=rules,
    )
    assert err is None
    assert qty == Decimal("0.050")

    # 2. Leverage capping: Ultra-tight stop (distance = $10) -> raw qty would be 100 / 10 = 10 BTC ($500k notional)
    # But max leverage = 3x ($30k notional) -> capped at 30,000 / 50,000 = 0.60 BTC
    qty_capped, err = PositionSizer.calculate_quantity(
        equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("1.0"),
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49990"),
        leverage=3,
        market_rules=rules,
    )
    assert err is None
    assert qty_capped == Decimal("0.600")

    # 3. Below min_notional reject:
    small_rules = rules.model_copy(update={"min_notional": Decimal("500.0")})
    qty_small, err = PositionSizer.calculate_quantity(
        equity=Decimal("100"),  # $1 risk
        risk_per_trade_pct=Decimal("1.0"),
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49000"),  # qty = 0.001 BTC ($50 notional < $500)
        leverage=1,
        market_rules=small_rules,
    )
    assert qty_small is None
    assert "minimum notional" in (err or "")


# =====================================================================
# 4. Paper Trading Broker Simulation Tests
# =====================================================================


async def test_paper_trading_broker_simulation() -> None:
    bus = EventBus()
    broker = PaperTradingBroker(
        event_bus=bus,
        initial_balance_usd=Decimal("10000.00"),
        taker_fee=Decimal("0.0005"),
        slippage_pct=Decimal("0.0005"),
    )

    req = NormalizedOrderRequest(
        client_order_id="P_ENT_001",
        symbol="BTC/USDT",
        market_type=MarketType.FUTURES_USDT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        purpose=OrderPurpose.ENTRY,
        quantity=Decimal("0.10"),
        stop_price=Decimal("63000.00"),
    )

    tp_target = TakeProfitTargetSpec(
        target_index=1,
        price=Decimal("68000.00"),
        close_Dynamic_pct=Decimal("100"),
    )

    # 1. Submit Market Entry Order
    resp = await broker.submit_order(
        request=req,
        current_market_price=Decimal("65000.00"),
        strategy_id="strat-01",
        tp_targets=[tp_target],
    )
    assert resp.status == OrderStatus.FILLED
    assert broker.open_positions_count == 1
    # Taker fee was deducted: notional ~6500 * 0.0005 = ~3.25
    assert broker.total_equity < Decimal("10000.00")

    # 2. Market price pumps to 68,500 (hits TP at 68,000)
    await broker.on_price_update(
        symbol="BTC/USDT",
        high=Decimal("68500.00"),
        low=Decimal("64800.00"),
        close=Decimal("68200.00"),
    )
    assert broker.open_positions_count == 0
    # Net realized gain realized: 0.10 * (68000 - 65000) ~ +300
    assert broker.total_equity > Decimal("10250.00")


# =====================================================================
# 5. Hybrid SL/TP Manager Tests
# =====================================================================


async def test_hybrid_sl_tp_manager_airbag_and_breakeven() -> None:
    bus = EventBus()
    manager = HybridSlTpManager(event_bus=bus)
    adapter = ExecutionMockAdapter()

    pos = Position(
        id="pos-777",
        strategy_id="strat-trend",
        symbol="BTC/USDT",
        market_type=MarketType.FUTURES_USDT.value,
        side=PositionSide.LONG.value,
        entry_price=Decimal("65000.00"),
        initial_qty=Decimal("0.10"),
        current_qty=Decimal("0.10"),
        stop_loss_price=Decimal("63700.00"),
        status=PositionStatus.OPEN.value,
    )

    tp1 = TakeProfitTargetSpec(
        target_index=1,
        price=Decimal("67000.00"),
        close_Dynamic_pct=Decimal("50"),
        move_sl_to_breakeven=True,
    )

    # 1. Place initial Tier-1 matching-engine hard stop
    resp = await manager.place_initial_protection(
        adapter=adapter,
        position=pos,
        tp_targets=[tp1],
    )
    assert len(adapter.placed_stops) == 1
    assert adapter.placed_stops[0]["stop_price"] == Decimal("63700.00")

    # 2. Price ticks to 67,200 (hits TP1 -> moves SL to Breakeven at 65,000)
    actions = await manager.on_market_tick(
        adapter=adapter,
        symbol="BTC/USDT",
        current_price=Decimal("67200.00"),
    )
    assert len(actions) == 1
    assert "BREAKEVEN" in actions[0]
    assert len(adapter.amended_stops) == 1
    assert adapter.amended_stops[0]["new_stop_price"] == Decimal("65000.00")


# =====================================================================
# 6. Startup Reconciler Tests
# =====================================================================


async def test_startup_reconciler_order_and_orphan_sync() -> None:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    session_factory = build_session_factory(engine)
    bus = EventBus()

    alerts: list[RiskAlertEvent] = []

    async def on_alert(ev: DomainEvent) -> None:
        if isinstance(ev, RiskAlertEvent):
            alerts.append(ev)

    bus.subscribe("risk:alert", on_alert)

    # Populate DB with an open position that was closed on exchange while offline
    async with session_factory() as session:
        strat = StrategyConfig(
            id="strat-01",
            name="Trend Strategy",
            strategy_class="ema_rsi_atr",
            symbols=["BTC/USDT"],
            timeframe="15m",
        )
        session.add(strat)

        pos = Position(
            id="pos-stale-001",
            strategy_id="strat-01",
            symbol="BTC/USDT",
            market_type=MarketType.FUTURES_USDT.value,
            side=PositionSide.LONG.value,
            entry_price=Decimal("65000.00"),
            initial_qty=Decimal("0.10"),
            current_qty=Decimal("0.10"),
            stop_loss_price=Decimal("63000.00"),
            status=PositionStatus.OPEN.value,
        )
        session.add(pos)
        await session.commit()

    # Exchange reports 0 open positions for BTC/USDT, but has an active orphan ETH/USDT position
    adapter = ExecutionMockAdapter()
    adapter.positions_to_return = [
        ExchangePositionSnapshot(
            symbol="ETH/USDT",
            market_type=MarketType.FUTURES_USDT,
            side=PositionSide.LONG,
            quantity=Decimal("2.0"),
            entry_price=Decimal("3100.00"),
            mark_price=Decimal("3150.00"),
        )
    ]

    reconciler = StartupReconciler(session_factory=session_factory, event_bus=bus)
    report = await reconciler.reconcile(adapter=adapter, market_type=MarketType.FUTURES_USDT)

    # 1. Stale BTC position marked RECONCILED_STALE
    assert report.stale_positions_closed_count == 1
    async with session_factory() as session:
        updated_pos = (
            await session.execute(select(Position).where(Position.id == "pos-stale-001"))
        ).scalar_one()
        assert updated_pos.status == PositionStatus.RECONCILED_STALE.value

    # 2. Orphan ETH position detected and critical alert published
    assert "ETH/USDT" in report.orphan_positions_detected
    assert len(alerts) >= 1
    assert alerts[0].rule_name == "ORPHAN_POSITION_DETECTED"

    await engine.dispose()
