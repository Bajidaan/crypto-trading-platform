"""Milestone 4 Test Suite: Indicators, Built-In Strategies, Webhooks, and Backtesting Engine."""

from datetime import datetime, timezone
from decimal import Decimal
import json
import time
import pytest

from app.core.events import DomainEvent, EventBus, MarketCandleEvent, PositionUpdatedEvent, TradeSignalEvent
from app.core.security import compute_webhook_signature
from app.domains.cex.adapters.base import CandleBar
from app.domains.cex.backtesting import DeterministicBacktestEngine
from app.domains.cex.market_data.buffer import CandleBuffer
from app.domains.cex.strategies import (
    BollingerAdxBreakoutStrategy,
    EmaRsiAtrTrendStrategy,
    IStrategy,
    SmartDcaScaleInStrategy,
    StrategyRegistry,
    WebhookSignalReceiver,
    calculate_adx,
    calculate_atr,
    calculate_bollinger_bands,
    calculate_ema,
    calculate_rsi,
)
from app.models.entities import StrategyConfig
from app.models.enums import (
    ExchangeId,
    ExecutionMode,
    MarketType,
    PositionSide,
    PositionStatus,
    SignalSource,
)


# =====================================================================
# 1. Indicator Precision Math Tests
# =====================================================================


def test_indicators_math() -> None:
    # 1. EMA test
    series = [Decimal(str(x)) for x in [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]]
    ema = calculate_ema(series, period=5)
    assert len(ema) == 7
    # First EMA is SMA(10, 11, 12, 13, 14) = 12
    assert ema[0] == Decimal("12")
    assert ema[-1] > ema[0]

    # 2. RSI test (strictly rising prices -> RSI = 100)
    rising = [Decimal(str(100 + i * 5)) for i in range(25)]
    rsi_rising = calculate_rsi(rising, period=14)
    assert len(rsi_rising) > 0
    assert rsi_rising[-1] == Decimal("100")

    # 3. ATR test
    highs = [Decimal(str(100 + i * 2)) for i in range(20)]
    lows = [Decimal(str(95 + i * 2)) for i in range(20)]
    closes = [Decimal(str(98 + i * 2)) for i in range(20)]
    atr = calculate_atr(highs, lows, closes, period=14)
    assert len(atr) > 0
    assert atr[-1] > Decimal("0")

    # 4. Bollinger Bands test
    close_vals = [Decimal(str(100 + (i % 5))) for i in range(30)]
    upper, mid, lower = calculate_bollinger_bands(close_vals, period=20, num_std=2.0)
    assert upper > mid > lower

    # 5. ADX test
    adx_val = calculate_adx(highs, lows, closes, period=14)
    assert adx_val >= Decimal("0")


# =====================================================================
# 2. Built-In Strategy Signal Tests
# =====================================================================


def test_ema_rsi_atr_strategy_signal_generation() -> None:
    strategy = EmaRsiAtrTrendStrategy(
        strategy_id="strat-ema-01",
        market_type=MarketType.FUTURES_USDT,
        parameters={
            "fast_ema_period": 3,
            "slow_ema_period": 5,
            "rsi_period": 5,
            "atr_period": 5,
            "rsi_long_threshold": 40.0,
            "only_on_candle_close": True,
        },
    )

    buffer = CandleBuffer(symbol="BTC/USDT", timeframe="15m", max_size=50)
    t_start = 1700000000

    # Build sequence leading into a bullish crossover with adequate warmup
    prices = [100] * 15 + [100, 99, 98, 97, 96, 95, 96, 98, 102, 108, 115]
    all_signals: list[TradeSignalEvent] = []

    for idx, p in enumerate(prices):
        bar = CandleBar(
            symbol="BTC/USDT",
            market_type=MarketType.FUTURES_USDT,
            timeframe="15m",
            open_time=datetime.fromtimestamp(t_start + idx * 900, tz=timezone.utc),
            open=Decimal(str(p - 1)),
            high=Decimal(str(p + 2)),
            low=Decimal(str(p - 2)),
            close=Decimal(str(p)),
            volume=Decimal("100"),
            is_closed=True,
        )
        buffer.update_or_append(bar)
        ev = MarketCandleEvent(
            exchange_id=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            market_type=MarketType.FUTURES_USDT,
            timeframe="15m",
            open_time=bar.open_time,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            is_closed=True,
        )
        signals = strategy.on_candle(ev, buffer)
        all_signals.extend(signals)

    assert len(all_signals) >= 1
    long_signals = [s for s in all_signals if s.side == PositionSide.LONG]
    assert len(long_signals) >= 1
    long_sig = long_signals[-1]
    assert long_sig.side == PositionSide.LONG
    assert long_sig.symbol == "BTC/USDT"
    assert long_sig.stop_loss_price < long_sig.signal_price
    assert len(long_sig.take_profit_targets) == 2
    assert long_sig.take_profit_targets[0].price > long_sig.signal_price
    assert long_sig.take_profit_targets[0].move_sl_to_breakeven is True


def test_bollinger_adx_breakout_strategy_signal_generation() -> None:
    strategy = BollingerAdxBreakoutStrategy(
        strategy_id="strat-bb-01",
        market_type=MarketType.FUTURES_USDT,
        parameters={
            "bb_period": 5,
            "bb_std": 1.5,
            "adx_period": 5,
            "adx_threshold": 10.0,
            "atr_period": 5,
            "only_on_candle_close": True,
        },
    )

    buffer = CandleBuffer(symbol="ETH/USDT", timeframe="15m", max_size=50)
    t_start = 1700000000

    # Low volatility compression followed by violent breakout with adequate warmup
    prices = [2000] * 15 + [2000, 2001, 2000, 2001, 2002, 2001, 2002, 2000, 2002, 2050]
    all_signals: list[TradeSignalEvent] = []

    for idx, p in enumerate(prices):
        bar = CandleBar(
            symbol="ETH/USDT",
            market_type=MarketType.FUTURES_USDT,
            timeframe="15m",
            open_time=datetime.fromtimestamp(t_start + idx * 900, tz=timezone.utc),
            open=Decimal(str(p)),
            high=Decimal(str(p + 5)),
            low=Decimal(str(p - 5)),
            close=Decimal(str(p)),
            volume=Decimal("50"),
            is_closed=True,
        )
        buffer.update_or_append(bar)
        ev = MarketCandleEvent(
            exchange_id=ExchangeId.BINANCE,
            symbol="ETH/USDT",
            market_type=MarketType.FUTURES_USDT,
            timeframe="15m",
            open_time=bar.open_time,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            is_closed=True,
        )
        signals = strategy.on_candle(ev, buffer)
        all_signals.extend(signals)

    assert len(all_signals) >= 1
    breakout_sig = all_signals[-1]
    assert breakout_sig.side == PositionSide.LONG
    assert breakout_sig.signal_price == Decimal("2050")
    assert breakout_sig.stop_loss_price < Decimal("2050")


def test_smart_dca_scale_in_strategy_lifecycle() -> None:
    strategy = SmartDcaScaleInStrategy(
        strategy_id="strat-dca-01",
        parameters={
            "rsi_period": 5,
            "base_entry_rsi_threshold": 50.0,
            "step_drop_pct": 2.0,
            "max_safety_orders": 2,
        },
    )

    buffer = CandleBuffer(symbol="SOL/USDT", timeframe="15m", max_size=50)
    t_start = 1700000000

    # Warmup baseline followed by oversold pullback and drop
    prices = [100] * 15 + [95, 90, 85, 80, 75, 70]
    all_signals: list[TradeSignalEvent] = []

    for idx, p in enumerate(prices):
        bar = CandleBar(
            symbol="SOL/USDT",
            market_type=MarketType.FUTURES_USDT,
            timeframe="15m",
            open_time=datetime.fromtimestamp(t_start + idx * 900, tz=timezone.utc),
            open=Decimal(str(p)),
            high=Decimal(str(p + 1)),
            low=Decimal(str(p - 1)),
            close=Decimal(str(p)),
            volume=Decimal("100"),
            is_closed=True,
        )
        buffer.update_or_append(bar)
        ev = MarketCandleEvent(
            exchange_id=ExchangeId.BINANCE,
            symbol="SOL/USDT",
            market_type=MarketType.FUTURES_USDT,
            timeframe="15m",
            open_time=bar.open_time,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            is_closed=True,
        )
        signals = strategy.on_candle(ev, buffer)
        all_signals.extend(signals)

    assert len(all_signals) >= 2
    assert all_signals[0].metadata.get("dca_order_type") == "BASE_ENTRY"
    assert all_signals[1].metadata.get("dca_order_type") == "SAFETY_SCALE_IN"

    # Reset strategy on position closed
    strategy.on_position_event(
        PositionUpdatedEvent(
            position_id="pos-001",
            strategy_id="strat-dca-01",
            symbol="SOL/USDT",
            market_type=MarketType.FUTURES_USDT,
            execution_mode=ExecutionMode.PAPER,
            side=PositionSide.LONG,
            status=PositionStatus.CLOSED,
            current_qty=Decimal("0"),
            entry_price=Decimal("75"),
            realized_pnl_usd=Decimal("15.0"),
        )
    )
    assert strategy._position_active is False


# =====================================================================
# 3. Strategy Registry & Webhook Receiver Tests
# =====================================================================


def test_strategy_registry_and_dynamic_plugin() -> None:
    available = StrategyRegistry.list_available()
    assert "ema_rsi_atr" in available
    assert "bb_breakout" in available
    assert "smart_dca" in available

    config = StrategyConfig(
        id="strat-cfg-001",
        name="My Trend Bot",
        strategy_class="ema_rsi_atr",
        symbols=["BTC/USDT"],
        timeframe="1h",
        parameters={"fast_ema_period": 10},
    )
    instance = StrategyRegistry.create_instance(config)
    assert isinstance(instance, EmaRsiAtrTrendStrategy)
    assert instance.fast_ema_period == 10


async def test_webhook_receiver_signature_and_normalization() -> None:
    bus = EventBus()
    secret = "test-webhook-secret-2026"
    receiver = WebhookSignalReceiver(event_bus=bus, webhook_secret=secret)

    received_signals: list[TradeSignalEvent] = []

    async def on_signal(ev: DomainEvent) -> None:
        if isinstance(ev, TradeSignalEvent):
            received_signals.append(ev)

    bus.subscribe("strategy:signal", on_signal)

    payload = {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "market_type": "FUTURES_USDT",
        "signal_price": 64500.0,
        "stop_loss": 63500.0,
        "take_profit_1": 66000.0,
        "take_profit_2": 67500.0,
        "metadata": {"source": "tradingview_pine_script"},
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = int(time.time())
    sig = compute_webhook_signature(secret, ts, raw_body)

    # 1. Verify HMAC
    assert receiver.verify_request(ts, raw_body, sig) is True

    # 2. Process and route signal
    signal_event = await receiver.process_signal(payload)
    assert signal_event.symbol == "BTC/USDT"
    assert signal_event.side == PositionSide.LONG
    assert signal_event.source == SignalSource.WEBHOOK
    assert len(signal_event.take_profit_targets) == 2
    assert len(received_signals) == 1


# =====================================================================
# 4. Deterministic Event-Driven Backtesting Engine Tests
# =====================================================================


def test_deterministic_backtest_engine_run() -> None:
    strategy = EmaRsiAtrTrendStrategy(
        strategy_id="backtest-strat-01",
        market_type=MarketType.FUTURES_USDT,
        parameters={
            "fast_ema_period": 3,
            "slow_ema_period": 6,
            "rsi_period": 5,
            "atr_period": 5,
            "rsi_long_threshold": 40.0,
            "atr_sl_multiplier": 1.5,
            "tp1_r_multiplier": 1.5,
            "only_on_candle_close": True,
        },
    )

    engine = DeterministicBacktestEngine(
        strategy=strategy,
        initial_capital=Decimal("10000.00"),
        slippage_pct=Decimal("0.0005"),
        risk_per_trade_pct=Decimal("1.0"),
    )

    # Create synthetic series: baseline 50 bars -> pump to 130 -> dump to 80
    candles: list[CandleBar] = []
    t_start = 1700000000
    prices = [100] * 10 + [100 + i for i in range(30)] + [130 - i * 2 for i in range(25)]

    for idx, p in enumerate(prices):
        candles.append(
            CandleBar(
                symbol="BTC/USDT",
                market_type=MarketType.FUTURES_USDT,
                timeframe="15m",
                open_time=datetime.fromtimestamp(t_start + idx * 900, tz=timezone.utc),
                open=Decimal(str(p - 1)),
                high=Decimal(str(p + 3)),
                low=Decimal(str(p - 3)),
                close=Decimal(str(p)),
                volume=Decimal("100"),
                is_closed=True,
            )
        )

    result = engine.run(candles)
    assert result.strategy_name == strategy.name
    assert result.metrics.initial_capital == Decimal("10000.00")
    assert result.metrics.total_trades >= 1
    assert len(result.equity_curve) >= 2
    assert result.metrics.win_rate_pct >= Decimal("0")
