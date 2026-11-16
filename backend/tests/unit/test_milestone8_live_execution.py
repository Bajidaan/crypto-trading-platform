"""Milestone 8: live execution wiring, Binance clock sync, algo stop-losses and the kline stream."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from app.core.database import build_async_engine, build_session_factory, init_database
from app.core.events import (
    DomainEvent,
    EventBus,
    MarketCandleEvent,
    RiskAlertEvent,
    TakeProfitTargetSpec,
    TradeSignalEvent,
)
from app.domains.cex.adapters.base import (
    AccountBalance,
    CandleBar,
    ExchangePositionSnapshot,
    ExchangeRateLimitError,
    IExchangeAdapter,
    MarketRules,
    NormalizedOrderRequest,
    NormalizedOrderResponse,
    OrderNotFoundError,
    TickerSnapshot,
)
from app.domains.cex.adapters.binance import BinanceAdapter
from app.domains.cex.execution import HybridSlTpManager, LiveExecutionBroker
from app.domains.cex.execution.runner import ContinuousStrategyRunner
from app.domains.cex.market_data.binance_ws import BinanceKlineStream, build_stream_url
from app.domains.cex.market_data.buffer import CandleBuffer
from app.domains.cex.risk import RiskGatekeeper
from app.domains.cex.strategies.base import IStrategy
from app.models.entities import ExchangeConnection, Order, Position, StrategyConfig
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
    SignalAction,
    SignalSource,
    TradingEnvironment,
)

FILL_PRICE = Decimal("65000")


# =====================================================================
# Fakes
# =====================================================================


class LiveMockAdapter(IExchangeAdapter):
    """Records every exchange call; stops can be made to fill or to fail."""

    def __init__(self) -> None:
        self.placed_orders: list[NormalizedOrderRequest] = []
        self.placed_stops: list[dict[str, Any]] = []
        self.canceled_stops: list[str] = []
        self.closed: list[dict[str, Any]] = []
        self.configured: list[tuple[str, int]] = []
        self.stop_status: dict[str, NormalizedOrderResponse] = {}
        self.fail_stop_placement = False
        self.synced = 0
        self.markets_loaded: list[MarketType] = []
        self.equity = Decimal("10000")

    @property
    def exchange_id(self) -> ExchangeId:
        return ExchangeId.BINANCE

    @property
    def is_testnet(self) -> bool:
        return True

    async def sync_server_time(self, market_type: MarketType = MarketType.FUTURES_USDT) -> int:
        self.synced += 1
        return 0

    async def load_markets(self, market_type: MarketType, force_refresh: bool = False) -> dict[str, MarketRules]:
        self.markets_loaded.append(market_type)
        return {}

    async def get_market_rules(self, symbol: str, market_type: MarketType) -> MarketRules:
        return MarketRules(
            symbol=symbol,
            exchange_symbol=symbol.replace("/", ""),
            market_type=market_type,
            base_asset=symbol.split("/")[0],
            quote_asset="USDT",
            tick_size=Decimal("0.10"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("10"),
        )

    async def fetch_balance(self, market_type: MarketType) -> AccountBalance:
        return AccountBalance(
            exchange_id=ExchangeId.BINANCE,
            market_type=market_type,
            total_equity=self.equity,
            available_balance=self.equity,
        )

    async def fetch_ticker(self, symbol: str, market_type: MarketType) -> TickerSnapshot:
        raise NotImplementedError

    async def fetch_ohlcv(self, symbol: str, market_type: MarketType, timeframe: str, since_ms: int | None = None, limit: int = 500) -> list[CandleBar]:
        return []

    async def configure_futures_symbol(self, symbol: str, leverage: int = 3, margin_mode: MarginMode = MarginMode.ISOLATED, one_way_mode: bool = True) -> None:
        self.configured.append((symbol, leverage))

    def _resp(self, req: NormalizedOrderRequest, status: OrderStatus = OrderStatus.FILLED) -> NormalizedOrderResponse:
        return NormalizedOrderResponse(
            client_order_id=req.client_order_id,
            exchange_order_id=f"ord-{len(self.placed_orders)}",
            symbol=req.symbol,
            market_type=req.market_type,
            side=req.side,
            order_type=req.order_type,
            status=status,
            requested_qty=req.quantity,
            filled_qty=req.quantity,
            avg_fill_price=FILL_PRICE,
            fee_usd=Decimal("2"),
        )

    async def place_order(self, request: NormalizedOrderRequest) -> NormalizedOrderResponse:
        self.placed_orders.append(request)
        return self._resp(request)

    async def place_hard_stop_loss(self, symbol: str, market_type: MarketType, side: OrderSide, quantity: Decimal, stop_price: Decimal, client_order_id: str, limit_price: Decimal | None = None) -> NormalizedOrderResponse:
        if self.fail_stop_placement:
            raise RuntimeError("stop rejected")
        self.placed_stops.append({"cid": client_order_id, "stop_price": stop_price, "qty": quantity, "side": side})
        resp = NormalizedOrderResponse(
            client_order_id=client_order_id,
            exchange_order_id=f"algo-{len(self.placed_stops)}",
            symbol=symbol,
            market_type=market_type,
            side=side,
            order_type=OrderType.STOP_MARKET,
            status=OrderStatus.OPEN,
            requested_qty=quantity,
            stop_price=stop_price,
            reduce_only=True,
        )
        self.stop_status[client_order_id] = resp
        return resp

    async def amend_or_replace_stop_loss(self, existing_order_id: str, existing_client_order_id: str, symbol: str, market_type: MarketType, side: OrderSide, quantity: Decimal, new_stop_price: Decimal, new_client_order_id: str) -> NormalizedOrderResponse:
        await self.cancel_stop_order(symbol, market_type, existing_order_id, existing_client_order_id)
        return await self.place_hard_stop_loss(symbol, market_type, side, quantity, new_stop_price, new_client_order_id)

    async def cancel_stop_order(self, symbol: str, market_type: MarketType, exchange_order_id: str | None = None, client_order_id: str | None = None) -> bool:
        self.canceled_stops.append(client_order_id or exchange_order_id or "")
        return True

    async def fetch_stop_order(self, symbol: str, market_type: MarketType, client_order_id: str) -> NormalizedOrderResponse:
        if client_order_id not in self.stop_status:
            raise OrderNotFoundError("unknown")
        return self.stop_status[client_order_id]

    async def cancel_order(self, exchange_order_id: str, symbol: str, market_type: MarketType, client_order_id: str | None = None) -> bool:
        return True

    async def cancel_all_orders(self, symbol: str, market_type: MarketType) -> int:
        return 0

    async def fetch_order(self, symbol: str, market_type: MarketType, exchange_order_id: str | None = None, client_order_id: str | None = None) -> NormalizedOrderResponse:
        raise OrderNotFoundError("unknown")

    async def fetch_open_orders(self, symbol: str | None, market_type: MarketType) -> list[NormalizedOrderResponse]:
        return []

    async def fetch_open_positions(self, market_type: MarketType, symbol: str | None = None) -> list[ExchangePositionSnapshot]:
        return []

    async def close_position_market(self, symbol: str, market_type: MarketType, side: PositionSide, quantity: Decimal, client_order_id: str) -> NormalizedOrderResponse:
        self.closed.append({"symbol": symbol, "side": side, "qty": quantity})
        return NormalizedOrderResponse(
            client_order_id=client_order_id,
            exchange_order_id="close-1",
            symbol=symbol,
            market_type=market_type,
            side=OrderSide.SELL if side == PositionSide.LONG else OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            requested_qty=quantity,
            filled_qty=quantity,
            avg_fill_price=Decimal("66000"),
            fee_usd=Decimal("2"),
        )

    async def close(self) -> None:
        pass


class StubAdapterFactory:
    """Returns `adapter` for every API key, or a per-connection adapter from `by_connection`."""

    def __init__(self, adapter: IExchangeAdapter, by_connection: dict[str, IExchangeAdapter] | None = None) -> None:
        self.adapter = adapter
        self.by_connection = by_connection or {}
        self.created = 0

    def create_from_connection(self, connection: ExchangeConnection, **_: Any) -> IExchangeAdapter:
        self.created += 1
        return self.by_connection.get(connection.id, self.adapter)

    def create_direct(self, **kwargs: Any) -> IExchangeAdapter:
        """Used when a new key is checked before saving. `direct` can return a per-call adapter."""
        self.direct_calls = getattr(self, "direct_calls", []) + [kwargs]
        direct = getattr(self, "direct", None)
        return direct(**kwargs) if direct else self.adapter


class CountingStrategy(IStrategy):
    """Emits a configurable list of signals on every closed bar and counts its evaluations."""

    def __init__(self, strategy_id: str, symbols: list[str], timeframe: str = "15m") -> None:
        super().__init__(
            strategy_id=strategy_id,
            name="counting",
            market_type=MarketType.FUTURES_USDT,
            symbols=symbols,
            timeframe=timeframe,
        )
        self.calls: list[datetime] = []
        self.to_emit: list[TradeSignalEvent] = []

    @property
    def required_warmup_bars(self) -> int:
        return 5

    def on_candle(self, candle: MarketCandleEvent, buffer: CandleBuffer) -> list[TradeSignalEvent]:
        self.calls.append(candle.open_time)
        return list(self.to_emit)


async def _setup(environment: TradingEnvironment = TradingEnvironment.DEMO) -> dict[str, Any]:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    session_factory = build_session_factory(engine)
    bus = EventBus()
    adapter = LiveMockAdapter()
    sl_tp = HybridSlTpManager(event_bus=bus)
    live_broker = LiveExecutionBroker(session_factory=session_factory, event_bus=bus, sl_tp_manager=sl_tp)
    runner = ContinuousStrategyRunner(
        session_factory=session_factory,
        event_bus=bus,
        environment=environment,
        risk_gatekeeper=RiskGatekeeper(event_bus=bus),
        adapter_factory=StubAdapterFactory(adapter),  # type: ignore[arg-type]
        live_broker=live_broker,
        sl_tp_manager=sl_tp,
        enable_kline_stream=False,
        market_data_adapter=adapter,
    )
    async with session_factory() as session:
        conn = ExchangeConnection(
            id="conn-1",
            exchange_id="BINANCE",
            label="main",
            api_key_masked="abc***",
            api_key_encrypted="x",
            api_secret_encrypted="y",
            environment=environment.value,
            futures_capital_cap_usd=Decimal("5000"),
        )
        cfg = StrategyConfig(
            id="strat-live-1",
            name="Live Trend",
            strategy_class="ema_rsi_atr",
            exchange_connection_id="conn-1",
            market_type=MarketType.FUTURES_USDT.value,
            symbols=["BTC/USDT"],
            timeframe="15m",
            parameters={},
            risk_config={"leverage": 3},
            is_active=True,
        )
        session.add_all([conn, cfg])
        await session.commit()
        await session.refresh(cfg)
    alerts: list[RiskAlertEvent] = []

    async def on_alert(ev: DomainEvent) -> None:
        if isinstance(ev, RiskAlertEvent):
            alerts.append(ev)

    bus.subscribe("risk:alert", on_alert)
    return {
        "session_factory": session_factory,
        "adapter": adapter,
        "runner": runner,
        "cfg": cfg,
        "bus": bus,
        "sl_tp": sl_tp,
        "alerts": alerts,
    }


def _entry_signal(action: SignalAction = SignalAction.ENTRY, tp: list[TakeProfitTargetSpec] | None = None) -> TradeSignalEvent:
    return TradeSignalEvent(
        strategy_id="strat-live-1",
        source=SignalSource.STRATEGY,
        symbol="BTC/USDT",
        side=PositionSide.LONG,
        action=action,
        target_weight=Decimal("0.5"),
        signal_price=FILL_PRICE,
        stop_loss_price=Decimal("63000"),
        take_profit_targets=tp or [],
    )


async def _positions(session_factory: Any) -> list[Position]:
    async with session_factory() as session:
        return list((await session.execute(select(Position))).scalars().all())


async def _orders(session_factory: Any) -> list[Order]:
    async with session_factory() as session:
        return list((await session.execute(select(Order))).scalars().all())


# =====================================================================
# Live routing through the runner
# =====================================================================


async def test_live_strategy_places_real_order_with_exchange_stop() -> None:
    env = await _setup()
    adapter: LiveMockAdapter = env["adapter"]

    await env["runner"]._process_signal(_entry_signal(), env["cfg"], FILL_PRICE)

    # Adapter prepared once: clock synced, rules preloaded, futures symbol configured at 3x.
    assert adapter.synced == 1
    assert MarketType.FUTURES_USDT in adapter.markets_loaded
    assert adapter.configured == [("BTC/USDT", 3)]

    # Entry sized from min(equity 10000, cap 5000) * 0.5 = 2500 USD -> 0.038 BTC (step 0.001).
    assert len(adapter.placed_orders) == 1
    entry = adapter.placed_orders[0]
    assert entry.order_type == OrderType.MARKET and entry.side == OrderSide.BUY
    assert entry.quantity == Decimal("0.038")
    assert len(adapter.placed_stops) == 1
    assert adapter.placed_stops[0]["stop_price"] == Decimal("63000")

    [pos] = await _positions(env["session_factory"])
    assert pos.execution_mode == ExecutionMode.DEMO.value
    assert pos.status == PositionStatus.OPEN.value
    assert pos.stop_order_live is True
    assert pos.active_stop_order_id == adapter.placed_stops[0]["cid"]
    purposes = sorted(o.purpose for o in await _orders(env["session_factory"]))
    assert purposes == [OrderPurpose.ENTRY.value, OrderPurpose.HARD_STOP_LOSS.value]


async def test_live_exit_cancels_stop_and_flattens() -> None:
    env = await _setup()
    adapter: LiveMockAdapter = env["adapter"]
    runner: ContinuousStrategyRunner = env["runner"]

    await runner._process_signal(_entry_signal(), env["cfg"], FILL_PRICE)
    stop_cid = adapter.placed_stops[0]["cid"]
    await runner._process_signal(_entry_signal(SignalAction.EXIT), env["cfg"], Decimal("66000"))

    assert adapter.canceled_stops == [stop_cid]
    assert len(adapter.closed) == 1
    [pos] = await _positions(env["session_factory"])
    assert pos.status == PositionStatus.CLOSED.value
    assert pos.exit_reason == "STRATEGY_EXIT"
    # (66000 - 65000) * 0.038 = 38, minus 2 entry + 2 exit fees.
    assert pos.realized_pnl_usd == Decimal("34")


async def test_duplicate_live_entry_blocked_by_concentration_guard() -> None:
    env = await _setup()
    await env["runner"]._process_signal(_entry_signal(), env["cfg"], FILL_PRICE)
    await env["runner"]._process_signal(_entry_signal(), env["cfg"], FILL_PRICE)
    assert len(env["adapter"].placed_orders) == 1


async def test_exchange_stop_fill_closes_position_locally() -> None:
    env = await _setup()
    adapter: LiveMockAdapter = env["adapter"]
    runner: ContinuousStrategyRunner = env["runner"]

    await runner._process_signal(_entry_signal(), env["cfg"], FILL_PRICE)
    stop_cid = adapter.placed_stops[0]["cid"]
    adapter.stop_status[stop_cid] = adapter.stop_status[stop_cid].model_copy(
        update={
            "status": OrderStatus.FILLED,
            "filled_qty": Decimal("0.038"),
            "avg_fill_price": Decimal("62990"),
            "fee_usd": Decimal("1"),
        }
    )

    await runner._sync_live_positions()

    [pos] = await _positions(env["session_factory"])
    assert pos.status == PositionStatus.CLOSED.value
    assert pos.exit_reason == "HARD_STOP_LOSS"
    assert env["sl_tp"].get_state(pos.id) is None
    stop_row = next(o for o in await _orders(env["session_factory"]) if o.purpose == OrderPurpose.HARD_STOP_LOSS.value)
    assert stop_row.status == OrderStatus.FILLED.value


async def test_canceled_exchange_stop_raises_critical_alert() -> None:
    env = await _setup()
    adapter: LiveMockAdapter = env["adapter"]
    await env["runner"]._process_signal(_entry_signal(), env["cfg"], FILL_PRICE)
    stop_cid = adapter.placed_stops[0]["cid"]
    adapter.stop_status[stop_cid] = adapter.stop_status[stop_cid].model_copy(update={"status": OrderStatus.CANCELED})

    await env["runner"]._sync_live_positions()

    [pos] = await _positions(env["session_factory"])
    assert pos.status == PositionStatus.OPEN.value
    assert pos.stop_order_live is False
    assert any(a.rule_name == "STOP_LOSS_MISSING" for a in env["alerts"])


async def test_failed_stop_placement_flattens_the_position() -> None:
    env = await _setup()
    adapter: LiveMockAdapter = env["adapter"]
    adapter.fail_stop_placement = True

    await env["runner"]._process_signal(_entry_signal(), env["cfg"], FILL_PRICE)

    assert len(adapter.closed) == 1
    [pos] = await _positions(env["session_factory"])
    assert pos.status == PositionStatus.CLOSED.value
    assert pos.exit_reason == "STOP_PLACEMENT_FAILED"


async def test_signal_without_environment_key_is_dropped_with_alert() -> None:
    env = await _setup()  # only a DEMO key exists
    runner: ContinuousStrategyRunner = env["runner"]
    runner.environment = TradingEnvironment.LIVE
    await runner._process_signal(_entry_signal(), env["cfg"], FILL_PRICE)
    assert env["adapter"].placed_orders == []
    assert any(a.rule_name == "ENVIRONMENT_CONNECTION_MISSING" for a in env["alerts"])


async def test_breakeven_move_is_persisted_for_stop_sync() -> None:
    env = await _setup()
    adapter: LiveMockAdapter = env["adapter"]
    runner: ContinuousStrategyRunner = env["runner"]
    tp = [TakeProfitTargetSpec(target_index=1, price=Decimal("67000"), move_sl_to_breakeven=True)]

    await runner._process_signal(_entry_signal(tp=tp), env["cfg"], FILL_PRICE)
    old_cid = adapter.placed_stops[0]["cid"]

    await runner._live_tick("BTC/USDT", Decimal("67100"))

    assert len(adapter.placed_stops) == 2
    assert adapter.placed_stops[1]["stop_price"] == FILL_PRICE
    [pos] = await _positions(env["session_factory"])
    assert pos.stop_loss_price == FILL_PRICE
    assert pos.active_stop_order_id == adapter.placed_stops[1]["cid"] != old_cid
    stops = [o for o in await _orders(env["session_factory"]) if o.purpose == OrderPurpose.HARD_STOP_LOSS.value]
    assert sorted(o.status for o in stops) == [OrderStatus.CANCELED.value, OrderStatus.OPEN.value]


async def test_restart_restores_tier2_protection() -> None:
    env = await _setup()
    tp = [TakeProfitTargetSpec(target_index=1, price=Decimal("67000"), move_sl_to_breakeven=True)]
    await env["runner"]._process_signal(_entry_signal(tp=tp), env["cfg"], FILL_PRICE)
    [pos] = await _positions(env["session_factory"])

    fresh = HybridSlTpManager(event_bus=EventBus())
    assert fresh.restore_protection(pos) is True
    state = fresh.get_state(pos.id)
    assert state is not None
    assert state.tp_targets[0].price == Decimal("67000")
    assert state.breakeven_triggered is False


async def _add_live_key(env: dict[str, Any]) -> LiveMockAdapter:
    """Add a LIVE Binance key whose adapter is separate from the DEMO one."""
    live_adapter = LiveMockAdapter()
    env["runner"].adapter_factory = StubAdapterFactory(env["adapter"], {"conn-live": live_adapter})
    async with env["session_factory"]() as session:
        session.add(ExchangeConnection(
            id="conn-live", exchange_id="BINANCE", label="real", api_key_masked="r***",
            api_key_encrypted="x", api_secret_encrypted="y", environment="LIVE",
            futures_capital_cap_usd=Decimal("5000"),
        ))
        await session.commit()
    return live_adapter


async def test_switching_environment_routes_new_entries_and_keeps_old_positions() -> None:
    env = await _setup()
    runner: ContinuousStrategyRunner = env["runner"]
    demo_adapter: LiveMockAdapter = env["adapter"]
    live_adapter = await _add_live_key(env)

    # Entry while on DEMO goes to the demo account.
    await runner._process_signal(_entry_signal(), env["cfg"], FILL_PRICE)
    assert len(demo_adapter.placed_orders) == 1 and live_adapter.placed_orders == []

    await runner.set_environment(TradingEnvironment.LIVE)
    eth = _entry_signal().model_copy(update={"symbol": "ETH/USDT", "stop_loss_price": Decimal("2900")})
    await runner._process_signal(eth, env["cfg"], Decimal("3000"))
    assert len(live_adapter.placed_orders) == 1 and len(demo_adapter.placed_orders) == 1

    positions = {p.symbol: p for p in await _positions(env["session_factory"])}
    assert positions["BTC/USDT"].execution_mode == "DEMO"
    assert positions["ETH/USDT"].execution_mode == "LIVE"
    assert runner.account["environment"] == "LIVE"

    # The DEMO position stays managed: its exchange stop filling still closes it.
    stop_cid = demo_adapter.placed_stops[0]["cid"]
    demo_adapter.stop_status[stop_cid] = demo_adapter.stop_status[stop_cid].model_copy(
        update={"status": OrderStatus.FILLED, "avg_fill_price": Decimal("62990")}
    )
    await runner._sync_live_positions()
    positions = {p.symbol: p for p in await _positions(env["session_factory"])}
    assert positions["BTC/USDT"].status == PositionStatus.CLOSED.value
    assert positions["ETH/USDT"].status == PositionStatus.OPEN.value


async def test_exit_signal_closes_position_on_its_own_environment() -> None:
    env = await _setup()
    runner: ContinuousStrategyRunner = env["runner"]
    demo_adapter: LiveMockAdapter = env["adapter"]
    live_adapter = await _add_live_key(env)

    await runner._process_signal(_entry_signal(), env["cfg"], FILL_PRICE)  # opened on DEMO
    await runner.set_environment(TradingEnvironment.LIVE)
    await runner._process_signal(_entry_signal(SignalAction.EXIT), env["cfg"], Decimal("66000"))

    assert len(demo_adapter.closed) == 1 and live_adapter.closed == []
    [pos] = await _positions(env["session_factory"])
    assert pos.status == PositionStatus.CLOSED.value and pos.execution_mode == "DEMO"


# =====================================================================
# Stream-driven evaluation
# =====================================================================


def _kline(open_time: datetime, close: str, closed: bool) -> MarketCandleEvent:
    return MarketCandleEvent(
        exchange_id=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        market_type=MarketType.FUTURES_USDT,
        timeframe="15m",
        open_time=open_time,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal("1"),
        is_closed=closed,
    )


async def _drain(runner: ContinuousStrategyRunner) -> None:
    while runner._eval_tasks:
        await asyncio.gather(*list(runner._eval_tasks), return_exceptions=True)


async def test_closed_kline_triggers_one_evaluation() -> None:
    env = await _setup()
    runner: ContinuousStrategyRunner = env["runner"]
    strat = CountingStrategy("strat-live-1", ["BTC/USDT"])
    runner._active_strategy_instances[env["cfg"].id] = strat
    runner._active_configs = [env["cfg"]]

    base = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    buf = runner._get_buffer("BTC/USDT", "15m", MarketType.FUTURES_USDT, 110)
    for i in range(60):
        buf.update_or_append(_kline(base - timedelta(minutes=15 * (60 - i)), "65000", True))

    await runner.on_kline(_kline(base, "65100", False))
    await _drain(runner)
    assert strat.calls == []  # in-progress bar never evaluates

    await runner.on_kline(_kline(base, "65200", True))
    await _drain(runner)
    assert strat.calls == [base]

    # The same close again (stream replay or poll catch-up) is not evaluated twice.
    await runner.on_kline(_kline(base, "65200", True))
    await runner._catch_up_streamed_symbol(strat, env["cfg"], "BTC/USDT", "15m", MarketType.FUTURES_USDT)
    await _drain(runner)
    assert strat.calls == [base]


async def test_streamed_close_signal_reaches_live_exchange() -> None:
    env = await _setup()
    runner: ContinuousStrategyRunner = env["runner"]
    strat = CountingStrategy("strat-live-1", ["BTC/USDT"])
    strat.to_emit = [_entry_signal()]
    runner._active_strategy_instances[env["cfg"].id] = strat
    runner._active_configs = [env["cfg"]]

    base = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    buf = runner._get_buffer("BTC/USDT", "15m", MarketType.FUTURES_USDT, 110)
    for i in range(60):
        buf.update_or_append(_kline(base - timedelta(minutes=15 * (60 - i)), "65000", True))

    await runner.on_kline(_kline(base, "65000", True))
    await _drain(runner)
    assert len(env["adapter"].placed_orders) == 1


class _FakeSocket:
    def __init__(self, frames: list[str]) -> None:
        self._frames = frames

    async def __aenter__(self) -> "_FakeSocket":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def __aiter__(self) -> "_FakeSocket":
        return self

    async def __anext__(self) -> str:
        if not self._frames:
            await asyncio.sleep(3600)
        return self._frames.pop(0)


async def test_kline_stream_parses_frames_and_reports_freshness() -> None:
    received: list[MarketCandleEvent] = []
    urls: list[str] = []
    frame = json.dumps(
        {
            "stream": "btcusdt@kline_15m",
            "data": {
                "e": "kline",
                "s": "BTCUSDT",
                "k": {"t": 1763294400000, "s": "BTCUSDT", "i": "15m", "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10", "x": True},
            },
        }
    )

    def connect(url: str) -> _FakeSocket:
        urls.append(url)
        return _FakeSocket([frame])

    async def on_kline(ev: MarketCandleEvent) -> None:
        received.append(ev)

    stream = BinanceKlineStream(on_kline=on_kline, connect_fn=connect)
    stream.start()
    key = ("BTC/USDT", "15m", MarketType.FUTURES_USDT)
    stream.set_subscriptions({key})
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.01)
    try:
        assert urls == ["wss://fstream.binance.com/market/stream?streams=btcusdt@kline_15m"]
        assert len(received) == 1 and received[0].is_closed and received[0].close == Decimal("1.5")
        assert stream.is_fresh(key)
        assert not stream.is_fresh(("ETH/USDT", "15m", MarketType.FUTURES_USDT))

        # Same subscriptions: no reconnect.
        stream.set_subscriptions({key})
        await asyncio.sleep(0.01)
        assert len(urls) == 1
    finally:
        await stream.stop()


def test_stream_urls_per_market() -> None:
    keys = {
        ("BTC/USDT", "15m", MarketType.SPOT),
        ("ETH/USDT", "1h", MarketType.SPOT),
        ("BTC/USDT", "15m", MarketType.FUTURES_USDT),
    }
    assert build_stream_url(MarketType.SPOT, keys) == (
        "wss://stream.binance.com:9443/stream?streams=btcusdt@kline_15m/ethusdt@kline_1h"
    )
    assert build_stream_url(MarketType.FUTURES_USDT, keys).endswith("streams=btcusdt@kline_15m")


# =====================================================================
# Binance adapter: clock sync, Retry-After, algo stop orders
# =====================================================================


def _binance(handler: Any, now_ms: int = 1_000_000) -> BinanceAdapter:
    return BinanceAdapter(
        api_key="K",
        api_secret="S",
        is_testnet=True,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        time_ms_fn=lambda: now_ms,
    )


async def test_timestamp_rejection_resyncs_clock_and_retries() -> None:
    server_ms = 1_003_000  # local clock 3 s behind Binance
    account_timestamps: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        qs = parse_qs(urlparse(str(request.url)).query)
        if path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": server_ms})
        if path == "/fapi/v2/account":
            ts = int(qs["timestamp"][0])
            account_timestamps.append(ts)
            if abs(ts - server_ms) > 1000:
                return httpx.Response(400, json={"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."})
            return httpx.Response(200, json={"totalMarginBalance": "100", "availableBalance": "100"})
        return httpx.Response(404, json={"code": -1000, "msg": path})

    adapter = _binance(handler)
    bal = await adapter.fetch_balance(MarketType.FUTURES_USDT)
    assert bal.total_equity == Decimal("100")
    assert account_timestamps == [1_000_000, 1_003_000]
    assert adapter.time_offset_ms == 3000


async def test_rate_limit_uses_retry_after_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418, headers={"Retry-After": "120"}, json={"code": -1003, "msg": "banned"})

    adapter = _binance(handler)
    with pytest.raises(ExchangeRateLimitError) as info:
        await adapter._request("GET", MarketType.FUTURES_USDT, "/fapi/v1/klines")
    assert info.value.retry_after_seconds == 120.0


async def test_algo_stop_lifecycle_on_futures() -> None:
    calls: list[tuple[str, str, dict[str, list[str]]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        qs = parse_qs(urlparse(str(request.url)).query)
        calls.append((request.method, path, qs))
        if path == "/fapi/v1/algoOrder" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "algoId": 77,
                    "clientAlgoId": qs["clientAlgoId"][0],
                    "algoType": "CONDITIONAL",
                    "orderType": "STOP_MARKET",
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "quantity": "0.038",
                    "algoStatus": "FINISHED",
                    "actualOrderId": "9001",
                    "actualPrice": "62990",
                    "triggerPrice": "63000",
                },
            )
        if path == "/fapi/v1/order" and request.method == "GET":
            assert qs["orderId"][0] == "9001"
            return httpx.Response(
                200,
                json={
                    "orderId": 9001,
                    "clientOrderId": "autogen",
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "type": "MARKET",
                    "status": "FILLED",
                    "origQty": "0.038",
                    "executedQty": "0.038",
                    "avgPrice": "62990.0",
                },
            )
        if path == "/fapi/v1/algoOrder" and request.method == "DELETE":
            return httpx.Response(200, json={"algoId": 77, "clientAlgoId": "x", "code": "200", "msg": "success"})
        if path == "/fapi/v1/openOrders":
            return httpx.Response(200, json=[])
        if path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(
                200,
                json=[{"algoId": 78, "clientAlgoId": "th_HAR_BTCUSD_1", "orderType": "STOP_MARKET", "symbol": "BTCUSDT", "side": "SELL", "quantity": "0.038", "algoStatus": "NEW", "triggerPrice": "63000"}],
            )
        return httpx.Response(404, json={"code": -1000, "msg": path})

    adapter = _binance(handler)

    # A fired stop reports the fill of the market order it became.
    stop = await adapter.fetch_stop_order("BTC/USDT", MarketType.FUTURES_USDT, "th_HAR_BTCUSD_1")
    assert stop.status == OrderStatus.FILLED
    assert stop.avg_fill_price == Decimal("62990.0")
    assert stop.client_order_id == "th_HAR_BTCUSD_1"

    # Cancelling a futures stop goes to the algo endpoint, by clientAlgoId.
    await adapter.cancel_stop_order("BTC/USDT", MarketType.FUTURES_USDT, client_order_id="th_HAR_BTCUSD_1")
    assert ("DELETE", "/fapi/v1/algoOrder") in [(m, p) for m, p, _ in calls]

    # Open orders include algo stops, so the reconciler sees the position as protected.
    open_orders = await adapter.fetch_open_orders("BTC/USDT", MarketType.FUTURES_USDT)
    assert [o.order_type for o in open_orders] == [OrderType.STOP_MARKET]
    assert open_orders[0].stop_price == Decimal("63000")
