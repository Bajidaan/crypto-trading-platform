"""Milestone 7 Test Suite: Volatility-Scaled Trend Strategy, Realistic Backtest Fills, Strategy Exits."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.core.database import build_async_engine, build_session_factory, init_database
from app.core.events import EventBus, MarketCandleEvent, TradeSignalEvent
from app.domains.cex.adapters.base import CandleBar
from app.domains.cex.backtesting import DeterministicBacktestEngine, fetch_ohlcv_history
from app.domains.cex.execution.runner import ContinuousStrategyRunner
from app.domains.cex.market_data.buffer import CandleBuffer
from app.domains.cex.risk.gatekeeper import RiskGatekeeper
from app.domains.cex.strategies import IStrategy, StrategyRegistry, VolScaledTrendStrategy
from app.models.entities import Position, StrategyConfig
from app.models.enums import ExchangeId, MarketType, PositionSide, SignalAction

T0 = datetime(2023, 1, 1, tzinfo=timezone.utc)
SLIP = Decimal("0.001")
FEE = Decimal("0.001")


def make_bars(closes: list[float], symbol: str = "BTC/USDT", opens: dict[int, float] | None = None) -> list[CandleBar]:
    """Daily bars where each bar opens at the previous close (unless overridden)."""
    bars: list[CandleBar] = []
    prev = closes[0]
    for i, close in enumerate(closes):
        open_ = (opens or {}).get(i, prev)
        bars.append(
            CandleBar(
                symbol=symbol,
                market_type=MarketType.FUTURES_USDT,
                timeframe="1d",
                open_time=T0 + timedelta(days=i),
                open=Decimal(str(open_)),
                high=Decimal(str(max(open_, close) * 1.01)),
                low=Decimal(str(min(open_, close) * 0.99)),
                close=Decimal(str(close)),
                volume=Decimal("1000"),
                is_closed=True,
            )
        )
        prev = close
    return bars


def candle_event(bar: CandleBar) -> MarketCandleEvent:
    return MarketCandleEvent(
        exchange_id=ExchangeId.BINANCE,
        symbol=bar.symbol,
        market_type=bar.market_type,
        timeframe=bar.timeframe,
        open_time=bar.open_time,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        is_closed=True,
    )


def buffer_of(bars: list[CandleBar]) -> CandleBuffer:
    buf = CandleBuffer(symbol=bars[0].symbol, timeframe="1d", max_size=1000)
    buf.load_history(bars)
    return buf


class ScriptedStrategy(IStrategy):
    """Enters on one bar index and exits on another, to pin down engine fill behaviour."""

    def __init__(
        self,
        enter_at: int,
        exit_at: int | None,
        stop: Decimal,
        target_weight: Decimal | None = None,
    ) -> None:
        super().__init__(strategy_id="scripted", name="Scripted", timeframe="1d")
        self.enter_at = enter_at
        self.exit_at = exit_at
        self.stop = stop
        self.weight = target_weight
        self._i = -1

    @property
    def required_warmup_bars(self) -> int:
        return 1

    def on_candle(self, candle: MarketCandleEvent, buffer: CandleBuffer) -> list[TradeSignalEvent]:
        self._i += 1
        if self._i == self.enter_at:
            return [
                self.create_signal(
                    symbol=candle.symbol,
                    side=PositionSide.LONG,
                    signal_price=candle.close,
                    stop_loss_price=self.stop,
                    target_weight=self.weight,
                )
            ]
        if self._i == self.exit_at and self.current_side(candle.symbol) == PositionSide.LONG:
            return [self.create_exit_signal(candle.symbol, PositionSide.LONG, candle.close)]
        return []


def small_trend_strategy(symbols: list[str] | None = None, **overrides: Any) -> VolScaledTrendStrategy:
    params: dict[str, Any] = {
        "fast_ma_period": 3,
        "slow_ma_period": 6,
        "regime_ma_period": 10,
        "vol_lookback": 5,
        "atr_period": 5,
        "band_pct": 1.0,
        "target_annual_vol_pct": 10,
    }
    params.update(overrides)
    return VolScaledTrendStrategy(
        strategy_id="vt-01", symbols=symbols or ["BTC/USDT"], timeframe="1d", parameters=params
    )


def rising(n: int, start: float = 100.0) -> list[float]:
    """Uptrend with uneven steps (+3%, +1%, ...) so realized volatility is not zero."""
    out = [start]
    for i in range(1, n):
        out.append(out[-1] * (1.03 if i % 2 else 1.01))
    return out


def falling(n: int, start: float) -> list[float]:
    out = [start]
    for i in range(1, n):
        out.append(out[-1] * (0.97 if i % 2 else 0.99))
    return out


# =====================================================================
# 1. Backtest engine: fills, sizing, exits, benchmark
# =====================================================================


def test_backtest_fills_at_next_bar_open_and_honours_strategy_exit() -> None:
    closes = [100, 101, 102, 110, 111, 112, 113, 120, 121, 122]
    bars = make_bars(closes)
    strat = ScriptedStrategy(enter_at=2, exit_at=6, stop=Decimal("50"), target_weight=Decimal("0.5"))
    engine = DeterministicBacktestEngine(strat, taker_fee=FEE, slippage_pct=SLIP)

    result = engine.run(bars)
    assert len(result.trades) == 1
    trade = result.trades[0]

    # Signal on bar 2 (close 102) fills at bar 3's open, not at bar 2's close.
    assert trade["entry_price"] == bars[3].open * (Decimal("1") + SLIP)
    assert trade["entry_time"] == bars[3].open_time.isoformat()
    # Exit signal on bar 6 fills at bar 7's open, with slippage against us.
    assert trade["exit_price"] == bars[7].open * (Decimal("1") - SLIP)
    assert trade["exit_reason"] == "STRATEGY_EXIT"

    # Target-weight sizing: half of equity as notional at the fill price.
    assert trade["quantity"] == Decimal("10000") * Decimal("0.5") / trade["entry_price"]
    # Fees charged on both legs.
    expected_fee = trade["quantity"] * (trade["entry_price"] + trade["exit_price"]) * FEE
    assert trade["fee_paid"] == expected_fee
    assert trade["net_pnl"] == trade["gross_pnl"] - expected_fee


def test_backtest_closes_open_position_at_end_and_reports_buy_and_hold() -> None:
    bars = make_bars(rising(40))
    strat = ScriptedStrategy(enter_at=5, exit_at=None, stop=Decimal("1"), target_weight=Decimal("1"))
    result = DeterministicBacktestEngine(strat, taker_fee=FEE, slippage_pct=SLIP).run(bars)

    assert len(result.trades) == 1
    assert result.trades[0]["exit_reason"] == "END_OF_DATA"
    assert len(result.bar_equity_curve) == len(bars)
    assert result.bar_equity_curve[-1] == result.equity_curve[-1]

    cmp = result.comparison
    assert cmp is not None
    # Buy-and-hold was invested for longer in a rising market, so it must come out ahead.
    assert cmp.buy_and_hold_return_pct > cmp.strategy_return_pct > Decimal("0")
    assert Decimal("0") < cmp.time_in_market_pct < Decimal("100")
    assert cmp.total_fees_paid == result.trades[0]["fee_paid"]


def test_backtest_stop_loss_gap_fills_at_open_not_at_stop() -> None:
    closes = [100, 100, 100, 100, 70, 70]
    # Bar 4 gaps down and opens at 80, well below the 90 stop.
    bars = make_bars(closes, opens={4: 80})
    strat = ScriptedStrategy(enter_at=1, exit_at=None, stop=Decimal("90"), target_weight=Decimal("1"))
    result = DeterministicBacktestEngine(strat, taker_fee=FEE, slippage_pct=SLIP).run(bars)

    trade = result.trades[0]
    assert trade["exit_reason"] == "STOP_LOSS"
    assert trade["exit_price"] == Decimal("80") * (Decimal("1") - SLIP)


# =====================================================================
# 2. Volatility-scaled trend strategy signals
# =====================================================================


def test_vol_trend_enters_long_in_uptrend_with_volatility_scaled_weight() -> None:
    bars = make_bars(rising(30))
    strat = small_trend_strategy()
    buf = buffer_of(bars)
    strat.context_buffers = {"BTC/USDT": buf}

    signals = strat.on_candle(candle_event(bars[-1]), buf)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == SignalAction.ENTRY
    assert sig.side == PositionSide.LONG
    assert sig.stop_loss_price < sig.signal_price
    assert sig.metadata["regime"] == "RISK_ON"

    # weight = target vol / realized vol, and realized vol here is well above the 10% target.
    assert sig.target_weight is not None
    assert Decimal("0") < sig.target_weight < Decimal("1")
    realized = Decimal(str(sig.metadata["annual_volatility_pct"])) / Decimal("100")
    assert abs(sig.target_weight - Decimal("0.10") / realized) < Decimal("0.0001")

    # A calmer target leaves a smaller position; the cap applies when the market is quiet.
    capped = small_trend_strategy(target_annual_vol_pct=500, max_weight_pct=60)
    capped.context_buffers = {"BTC/USDT": buf}
    assert capped.on_candle(candle_event(bars[-1]), buf)[0].target_weight == Decimal("0.6")


def test_vol_trend_holds_then_exits_when_trend_turns_down() -> None:
    closes = rising(30)
    closes += falling(12, closes[-1])
    bars = make_bars(closes)
    strat = small_trend_strategy(use_regime_filter=0)

    actions: list[tuple[int, SignalAction]] = []
    held: PositionSide | None = None
    for i in range(len(bars)):
        strat.position_state = {"BTC/USDT": held} if held else {}
        for sig in strat.on_candle(candle_event(bars[i]), buffer_of(bars[: i + 1])):
            actions.append((i, sig.action))
            held = PositionSide.LONG if sig.action == SignalAction.ENTRY else None

    assert [a for _, a in actions] == [SignalAction.ENTRY, SignalAction.EXIT]
    entry_bar, exit_bar = actions[0][0], actions[1][0]
    assert entry_bar < 30 <= exit_bar  # exit only after the market turned


def test_vol_trend_regime_filter_blocks_entry_and_forces_exit() -> None:
    eth = make_bars(rising(30), symbol="ETH/USDT")
    btc_down = make_bars(falling(30, 100.0), symbol="BTC/USDT")
    strat = small_trend_strategy(symbols=["ETH/USDT"])
    strat.context_buffers = {"BTC/USDT": buffer_of(btc_down)}
    assert strat.context_symbols == ["BTC/USDT"]

    # ETH is trending up but the benchmark is below its long average: stay in cash.
    assert strat.on_candle(candle_event(eth[-1]), buffer_of(eth)) == []

    # Already long when the benchmark breaks down: rotate to cash.
    strat.position_state = {"ETH/USDT": PositionSide.LONG}
    signals = strat.on_candle(candle_event(eth[-1]), buffer_of(eth))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT
    assert signals[0].metadata["exit_trigger"] == "REGIME_OFF"

    # No benchmark data at all: never open a position on an unknown regime.
    strat.position_state = {}
    strat.context_buffers = {}
    assert strat.on_candle(candle_event(eth[-1]), buffer_of(eth)) == []


def test_vol_trend_no_trade_band_ignores_small_moves() -> None:
    # A drift of 0.1% per bar keeps the fast average inside the 1% band around the slow one.
    closes = [100 * (1.001**i) for i in range(30)]
    bars = make_bars(closes)
    buf = buffer_of(bars)

    banded = small_trend_strategy(use_regime_filter=0)
    assert banded.on_candle(candle_event(bars[-1]), buf) == []
    banded.position_state = {"BTC/USDT": PositionSide.LONG}
    assert banded.on_candle(candle_event(bars[-1]), buf) == []  # and no exit either

    no_band = small_trend_strategy(use_regime_filter=0, band_pct=0)
    assert len(no_band.on_candle(candle_event(bars[-1]), buf)) == 1


def test_vol_trend_ignores_bar_still_in_progress() -> None:
    bars = make_bars(rising(30))
    live = bars[-1].model_copy(update={"is_closed": False, "close": Decimal("1")})
    buf = buffer_of(bars[:-1] + [live])
    strat = small_trend_strategy(use_regime_filter=0)
    # Evaluating the last closed bar must not see the collapsing unfinished bar.
    signals = strat.on_candle(candle_event(bars[-2]), buf)
    assert len(signals) == 1 and signals[0].side == PositionSide.LONG


def test_vol_trend_backtest_cuts_drawdown_in_bull_then_bear_market() -> None:
    closes = rising(120)
    closes += falling(120, closes[-1])
    bars = make_bars(closes)
    strat = small_trend_strategy(target_annual_vol_pct=500)  # full size, to compare like for like
    result = DeterministicBacktestEngine(strat, taker_fee=FEE, slippage_pct=SLIP).run(bars)

    assert "vol_trend" in StrategyRegistry.list_available()
    assert result.metrics.total_trades >= 1
    assert any(t["exit_reason"] == "STRATEGY_EXIT" for t in result.trades)
    cmp = result.comparison
    assert cmp is not None
    assert cmp.strategy_max_drawdown_pct < cmp.buy_and_hold_max_drawdown_pct
    assert cmp.strategy_return_pct > cmp.buy_and_hold_return_pct


# =====================================================================
# 3. Paginated history download
# =====================================================================


class PagingAdapter:
    def __init__(self, bars: list[CandleBar]) -> None:
        self.bars = bars
        self.calls = 0

    async def fetch_ohlcv(
        self, symbol: str, market_type: MarketType, timeframe: str, since_ms: int | None = None, limit: int = 500
    ) -> list[CandleBar]:
        self.calls += 1
        since = since_ms or 0
        return [b for b in self.bars if b.open_time.timestamp() * 1000 >= since][:limit]


async def test_fetch_ohlcv_history_pages_and_drops_unfinished_bar() -> None:
    bars = make_bars([100.0 + i for i in range(2500)])
    bars[-1] = bars[-1].model_copy(update={"is_closed": False})
    adapter = PagingAdapter(bars)
    now_ms = int((bars[-1].open_time + timedelta(hours=6)).timestamp() * 1000)

    history = await fetch_ohlcv_history(
        adapter, "BTC/USDT", MarketType.FUTURES_USDT, "1d", total_bars=1200, now_ms=now_ms  # type: ignore[arg-type]
    )
    assert adapter.calls >= 2
    assert len(history) == 1200
    assert all(b.is_closed for b in history)
    assert history[-1].open_time == bars[-2].open_time
    gaps = {history[i + 1].open_time - history[i].open_time for i in range(len(history) - 1)}
    assert gaps == {timedelta(days=1)}


# =====================================================================
# 4. Runner: target-weight entry and strategy-driven exit on the demo account
# =====================================================================


async def _demo_runner(session_factory: Any, bus: EventBus) -> tuple[ContinuousStrategyRunner, Any]:
    """Runner trading on a mocked Binance Demo Trading account with $10,000 equity."""
    from app.domains.cex.execution import HybridSlTpManager, LiveExecutionBroker
    from app.models.entities import ExchangeConnection
    from tests.unit.test_milestone8_live_execution import LiveMockAdapter, StubAdapterFactory

    adapter = LiveMockAdapter()
    async with session_factory() as session:
        session.add(
            ExchangeConnection(
                id="demo-conn", exchange_id="BINANCE", label="demo", api_key_masked="x***",
                api_key_encrypted="x", api_secret_encrypted="y", environment="DEMO",
                futures_capital_cap_usd=Decimal("0"),  # no cap: size from the whole balance
            )
        )
        await session.commit()
    sl_tp = HybridSlTpManager(event_bus=bus)
    runner = ContinuousStrategyRunner(
        session_factory=session_factory,
        event_bus=bus,
        risk_gatekeeper=RiskGatekeeper(event_bus=bus),
        adapter_factory=StubAdapterFactory(adapter),  # type: ignore[arg-type]
        live_broker=LiveExecutionBroker(session_factory=session_factory, event_bus=bus, sl_tp_manager=sl_tp),
        sl_tp_manager=sl_tp,
        enable_kline_stream=False,
        market_data_adapter=adapter,
    )
    return runner, adapter


async def test_runner_sizes_by_target_weight_and_closes_on_exit_signal() -> None:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    session_factory = build_session_factory(engine)
    bus = EventBus()

    cfg = StrategyConfig(
        id="vt-live-01",
        name="Vol Trend",
        strategy_class="vol_trend",
        market_type=MarketType.FUTURES_USDT.value,
        symbols=["BTC/USDT"],
        timeframe="1d",
        parameters={},
        risk_config={},
    )
    async with session_factory() as session:
        session.add(cfg)
        await session.commit()

    runner, adapter = await _demo_runner(session_factory, bus)
    strat = StrategyRegistry.create_instance(cfg)

    entry = strat.create_signal(
        symbol="BTC/USDT",
        side=PositionSide.LONG,
        signal_price=Decimal("100"),
        stop_loss_price=Decimal("80"),
        target_weight=Decimal("0.75"),
    )
    await runner._process_signal(entry, cfg, Decimal("100"))

    # 75% of $10,000 at $100, not capped by the default $5,000 notional limit.
    assert len(adapter.placed_orders) == 1
    assert adapter.placed_orders[0].quantity == Decimal("75")

    exit_sig = strat.create_exit_signal("BTC/USDT", PositionSide.LONG, Decimal("120"))
    await runner._process_signal(exit_sig, cfg, Decimal("120"))
    assert len(adapter.closed) == 1

    async with session_factory() as session:
        db_pos = (await session.execute(select(Position))).scalars().one()
        assert db_pos.status == "CLOSED"
        assert db_pos.exit_reason == "STRATEGY_EXIT"
        assert db_pos.execution_mode == "DEMO"

    await engine.dispose()


# =====================================================================
# 5. Take-profit targets survive persistence and a restart
# =====================================================================


async def test_take_profit_percentages_persist_and_restore() -> None:
    from app.core.events import TakeProfitTargetSpec
    from app.domains.cex.execution import HybridSlTpManager

    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    session_factory = build_session_factory(engine)
    bus = EventBus()

    cfg = StrategyConfig(
        id="tp-01",
        name="EMA Trend",
        strategy_class="ema_rsi_atr",
        market_type=MarketType.FUTURES_USDT.value,
        symbols=["BTC/USDT"],
        timeframe="15m",
        parameters={},
        risk_config={},
    )
    async with session_factory() as session:
        session.add(cfg)
        await session.commit()

    runner, _ = await _demo_runner(session_factory, bus)
    strat = StrategyRegistry.create_instance(cfg)
    entry = strat.create_signal(
        symbol="BTC/USDT",
        side=PositionSide.LONG,
        signal_price=Decimal("100"),
        stop_loss_price=Decimal("95"),
        take_profit_targets=[
            TakeProfitTargetSpec(target_index=1, price=Decimal("110"), close_dynamic_pct=Decimal("50")),
            TakeProfitTargetSpec(target_index=2, price=Decimal("120"), close_dynamic_pct=Decimal("50")),
        ],
    )
    await runner._process_signal(entry, cfg, Decimal("100"))

    # The position is written to the database with its take-profit percentages.
    async with session_factory() as session:
        db_pos = (await session.execute(select(Position))).scalars().one()
        assert [t["close_dynamic_pct"] for t in db_pos.take_profit_state] == ["50", "50"]

    # After a restart the protection state is rebuilt with the same percentages.
    restarted = HybridSlTpManager(event_bus=bus)
    assert restarted.restore_protection(db_pos) is True
    state = restarted.get_state(db_pos.id)
    assert state is not None
    assert [t.close_dynamic_pct for t in state.tp_targets] == [Decimal("50"), Decimal("50")]

    await engine.dispose()
