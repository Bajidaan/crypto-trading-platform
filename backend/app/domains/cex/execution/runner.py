"""Continuous Background Strategy Runner and Real-Time Market Feeder.

Strategies are evaluated the moment Binance reports a candle closed (kline WebSocket). A
REST poll every `poll_interval_seconds` refreshes the strategy list, keeps the stream
subscriptions current, polls any symbol whose stream is stale, and checks live exchange
stops for fills. Signals go to the paper broker or, for strategies in LIVE mode, to the
exchange through `LiveExecutionBroker`.
"""

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
import logging
import time
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.decimal_utils import to_decimal
from app.core.events import (
    EventBus,
    MarketCandleEvent,
    PositionUpdatedEvent,
    RiskAlertEvent,
    TradeSignalEvent,
)
from app.domains.cex.adapters.base import (
    CandleBar,
    IExchangeAdapter,
    MarketRules,
    NormalizedOrderRequest,
)
from app.domains.cex.adapters.binance import BinanceAdapter
from app.domains.cex.adapters.factory import ExchangeAdapterFactory
from app.domains.cex.execution.live_broker import LiveExecutionBroker
from app.domains.cex.execution.paper_broker import PaperTradingBroker
from app.domains.cex.execution.sl_tp_manager import HybridSlTpManager
from app.domains.cex.market_data.binance_ws import BinanceKlineStream, KlineKey
from app.domains.cex.market_data.buffer import CandleBuffer
from app.domains.cex.risk.gatekeeper import RiskGatekeeper
from app.domains.cex.risk.position_sizer import PositionSizer
from app.domains.cex.strategies.base import IStrategy
from app.domains.cex.strategies.registry import StrategyRegistry
from app.models.entities import ExchangeConnection, Fill, Order, Position, StrategyConfig
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
)

logger = logging.getLogger(__name__)

# Live ticks arrive every ~250 ms per futures stream; dashboard updates, paper SL/TP checks
# and Tier-2 checks are rate-limited to this interval per kline.
TICK_THROTTLE_SEC = 1.0


class ContinuousStrategyRunner:
    """Continuously evaluates active strategies against live market candles and executes paper/live trades."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_bus: EventBus,
        paper_broker: PaperTradingBroker,
        risk_gatekeeper: RiskGatekeeper,
        adapter_factory: ExchangeAdapterFactory,
        poll_interval_seconds: float = 15.0,
        live_broker: LiveExecutionBroker | None = None,
        sl_tp_manager: HybridSlTpManager | None = None,
        enable_kline_stream: bool = True,
        kline_stream_factory: Callable[..., BinanceKlineStream] | None = None,
        market_data_adapter: IExchangeAdapter | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.event_bus = event_bus
        self.paper_broker = paper_broker
        self.risk_gatekeeper = risk_gatekeeper
        self.adapter_factory = adapter_factory
        self.poll_interval = poll_interval_seconds
        self.live_broker = live_broker
        self.sl_tp_manager = sl_tp_manager or (live_broker.sl_tp_manager if live_broker else None)
        self._enable_stream = enable_kline_stream
        self._stream_factory = kline_stream_factory or BinanceKlineStream

        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._buffers: dict[tuple[str, str, str], CandleBuffer] = {}
        self._active_strategy_instances: dict[str, IStrategy] = {}
        self._active_configs: list[StrategyConfig] = []
        self._evaluated_bars: set[tuple[str, str, str]] = set()
        self._eval_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._eval_tasks: set[asyncio.Task[Any]] = set()
        self._default_adapter: IExchangeAdapter | None = market_data_adapter
        self._owns_default_adapter = market_data_adapter is None
        self._stream: BinanceKlineStream | None = None
        self._last_tick_at: dict[KlineKey, float] = {}

        # Live trading state, per exchange connection id.
        self._live_adapters: dict[str, IExchangeAdapter] = {}
        self._live_conn_caps: dict[str, dict[MarketType, Decimal]] = {}
        self._live_lock = asyncio.Lock()
        self._futures_configured: set[tuple[str, str, int]] = set()
        self._day_start_equity: dict[tuple[str, MarketType], tuple[date, Decimal]] = {}
        self._position_connection: dict[str, str] = {}

        self.event_bus.subscribe(PositionUpdatedEvent, self._handle_position_closed)

    # ================================================================== lifecycle

    def start(self) -> None:
        """Start the background strategy evaluation loop and the kline stream."""
        if self._running:
            return
        self._running = True
        if self._default_adapter is None:
            self._default_adapter = BinanceAdapter(api_key="", api_secret="", is_testnet=False)
        if self._enable_stream:
            self._stream = self._stream_factory(on_kline=self.on_kline)
            self._stream.start()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "ContinuousStrategyRunner started (kline stream: %s, poll interval: %ss).",
            "on" if self._stream else "off",
            self.poll_interval,
        )

    async def stop(self) -> None:
        """Gracefully terminate background runner loop, streams and exchange clients."""
        self._running = False
        if self._stream:
            await self._stream.stop()
            self._stream = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for task in list(self._eval_tasks):
            task.cancel()
        if self._default_adapter and self._owns_default_adapter:
            await self._default_adapter.close()
            self._default_adapter = None
        for adapter in self._live_adapters.values():
            try:
                await adapter.close()
            except Exception:
                pass
        self._live_adapters.clear()
        logger.info("ContinuousStrategyRunner stopped.")

    @property
    def stream_stats(self) -> dict[str, Any] | None:
        return self._stream.stats if self._stream else None

    async def _handle_position_closed(self, event: PositionUpdatedEvent) -> None:
        """Notify strategy instances when a position is closed so stateful strategies (e.g. Smart DCA) reset."""
        if event.status == PositionStatus.CLOSED:
            self._position_connection.pop(event.position_id, None)
            for strat in self._active_strategy_instances.values():
                if hasattr(strat, "on_position_event"):
                    try:
                        strat.on_position_event(event)
                    except Exception as exc:
                        logger.debug("Strategy on_position_event callback error: %s", exc)

    # ================================================================== buffers

    def _get_buffer(
        self,
        symbol: str,
        timeframe: str,
        market_type: MarketType,
        min_size: int = 300,
    ) -> CandleBuffer:
        key = (symbol, timeframe, market_type.value)
        existing = self._buffers.get(key)
        if existing is None or existing.max_size < min_size:
            grown = CandleBuffer(
                symbol=symbol,
                timeframe=timeframe,
                market_type=market_type,
                max_size=max(300, min_size),
            )
            if existing is not None:
                grown.load_history(existing.candles)
            self._buffers[key] = grown
        return self._buffers[key]

    def _stream_fresh(self, symbol: str, timeframe: str, market_type: MarketType) -> bool:
        return bool(self._stream and self._stream.is_fresh((symbol, timeframe, market_type)))

    async def _ensure_warm(
        self, buffer: CandleBuffer, symbol: str, timeframe: str, market_type: MarketType, limit: int
    ) -> bool:
        """Load history over REST once, so streamed candles extend a full indicator window."""
        if buffer.count >= limit:
            return True
        try:
            candles = await self._default_adapter.fetch_ohlcv(  # type: ignore[union-attr]
                symbol=symbol, market_type=market_type, timeframe=timeframe, limit=limit
            )
        except Exception as err:
            logger.warning("Warm-up fetch failed for %s %s: %s", symbol, timeframe, err)
            return False
        for c in candles:
            buffer.update_or_append(c)
        return buffer.count > 0

    async def _load_context_buffers(
        self,
        strat: IStrategy,
        symbol: str,
        symbol_buffer: CandleBuffer,
        timeframe: str,
        market_type: MarketType,
        limit: int,
        closed_candle: CandleBar | None = None,
    ) -> bool:
        """Give the strategy fresh candles for its context symbols (e.g. a benchmark).

        Streamed context buffers are used as-is when they already hold the bar being
        evaluated; otherwise the candles are fetched over REST. Returns False when a context
        symbol could not be loaded, so the caller can retry later instead of evaluating the
        strategy on missing data.
        """
        for ctx_symbol in strat.context_symbols:
            if ctx_symbol == symbol:
                strat.context_buffers[ctx_symbol] = symbol_buffer
                continue
            ctx_buffer = self._get_buffer(ctx_symbol, timeframe, market_type, limit + 50)
            ctx_closed = ctx_buffer.latest_closed
            if (
                closed_candle is not None
                and ctx_buffer.count >= limit
                and ctx_closed is not None
                and ctx_closed.open_time >= closed_candle.open_time
            ):
                strat.context_buffers[ctx_symbol] = ctx_buffer
                continue
            try:
                ctx_candles = await self._default_adapter.fetch_ohlcv(  # type: ignore[union-attr]
                    symbol=ctx_symbol,
                    market_type=market_type,
                    timeframe=timeframe,
                    limit=limit,
                )
            except Exception as err:
                logger.warning("Failed to fetch context candles for %s: %s", ctx_symbol, err)
                return False
            if not ctx_candles:
                return False
            for c in ctx_candles:
                ctx_buffer.update_or_append(c)
            strat.context_buffers[ctx_symbol] = ctx_buffer
        return True

    # ================================================================== poll loop

    async def _run_loop(self) -> None:
        """Periodic loop: refresh strategies and subscriptions, poll stale symbols, sync live stops."""
        # Initial wait to let system finish boot
        await asyncio.sleep(2.0)

        while self._running:
            try:
                await self._evaluate_active_strategies()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in ContinuousStrategyRunner cycle: %s", exc, exc_info=True)

            try:
                await self._sync_live_positions()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Live position sync failed: %s", exc, exc_info=True)

            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    async def _refresh_active_configs(self) -> list[StrategyConfig]:
        async with self.session_factory() as session:
            stmt = select(StrategyConfig).where(StrategyConfig.is_active == True)  # noqa: E712
            active_configs = list((await session.execute(stmt)).scalars().all())

        for cfg in active_configs:
            strat = self._active_strategy_instances.get(cfg.id)
            if not strat or strat.parameters != cfg.parameters or strat.timeframe != cfg.timeframe:
                try:
                    self._active_strategy_instances[cfg.id] = StrategyRegistry.create_instance(cfg)
                except Exception as exc:
                    logger.warning("Could not build strategy %s (%s): %s", cfg.name, cfg.id, exc)
        self._active_configs = [c for c in active_configs if c.id in self._active_strategy_instances]

        if self._stream:
            keys: set[KlineKey] = set()
            for cfg in self._active_configs:
                strat = self._active_strategy_instances[cfg.id]
                mt = MarketType(cfg.market_type)
                tf = cfg.timeframe or "15m"
                for sym in list(cfg.symbols or ["BTC/USDT"]) + list(strat.context_symbols):
                    keys.add((sym, tf, mt))
            self._stream.set_subscriptions(keys)
        return self._active_configs

    async def _evaluate_active_strategies(self) -> None:
        """Refresh strategies; poll symbols whose stream is stale and catch up on missed closes."""
        active_configs = await self._refresh_active_configs()
        for cfg in active_configs:
            try:
                strat = self._active_strategy_instances[cfg.id]
                symbols = cfg.symbols or ["BTC/USDT"]
                market_type = MarketType(cfg.market_type)
                timeframe = cfg.timeframe or "15m"

                for sym in symbols:
                    if self._stream_fresh(sym, timeframe, market_type):
                        await self._catch_up_streamed_symbol(strat, cfg, sym, timeframe, market_type)
                    else:
                        await self._evaluate_symbol_strategy(
                            strat=strat,
                            cfg=cfg,
                            symbol=sym,
                            timeframe=timeframe,
                            market_type=market_type,
                        )
            except Exception as exc:
                logger.warning("Strategy evaluation failed for %s (%s): %s", cfg.name, cfg.id, exc)

    async def _catch_up_streamed_symbol(
        self,
        strat: IStrategy,
        cfg: StrategyConfig,
        symbol: str,
        timeframe: str,
        market_type: MarketType,
    ) -> None:
        """Stream is live: make sure history is loaded and no closed bar was skipped."""
        limit = max(50, strat.required_warmup_bars + 10)
        buffer = self._get_buffer(symbol, timeframe, market_type, limit + 50)
        if not await self._ensure_warm(buffer, symbol, timeframe, market_type, limit):
            return
        closed = buffer.latest_closed
        latest = buffer.latest
        if closed is None or latest is None:
            return
        # Already-evaluated bars are skipped inside, so this only acts if the close event was missed.
        await self._evaluate_closed_bar(
            strat, cfg, symbol, timeframe, market_type, buffer, closed, latest.close, limit
        )

    async def _evaluate_symbol_strategy(
        self,
        strat: IStrategy,
        cfg: StrategyConfig,
        symbol: str,
        timeframe: str,
        market_type: MarketType,
    ) -> None:
        """REST fallback: fetch latest candles, update buffer, check SL/TP, and process signals."""
        adapter = self._default_adapter
        if not adapter:
            return

        # 1. Fetch recent candles from exchange (50 bars for indicator warmup)
        limit = max(50, strat.required_warmup_bars + 10)
        buffer = self._get_buffer(symbol, timeframe, market_type, limit + 50)
        try:
            candles = await adapter.fetch_ohlcv(
                symbol=symbol,
                market_type=market_type,
                timeframe=timeframe,
                limit=limit,
            )
        except Exception as err:
            logger.debug("Failed to fetch live OHLCV for %s: %s", symbol, err)
            return

        if not candles:
            return

        # 2. Update buffer with candles
        for c in candles:
            buffer.update_or_append(c)

        latest_candle = candles[-1]

        # 3. Trigger SL/TP checks on active paper positions using latest high/low
        await self.paper_broker.on_price_update(
            symbol=symbol,
            high=latest_candle.high,
            low=latest_candle.low,
            close=latest_candle.close,
        )

        # 3b. Broadcast live candle event for dashboard charts and ticker displays
        await self.event_bus.publish(
            MarketCandleEvent(
                exchange_id=ExchangeId.BINANCE,
                symbol=symbol,
                market_type=market_type,
                timeframe=timeframe,
                open_time=latest_candle.open_time,
                open=latest_candle.open,
                high=latest_candle.high,
                low=latest_candle.low,
                close=latest_candle.close,
                volume=latest_candle.volume,
                is_closed=latest_candle.is_closed,
            )
        )

        # 4. Evaluate strategy on latest CLOSED candle (candles[-1] is still in progress)
        closed_candle = next((c for c in reversed(candles) if c.is_closed), None)
        if not closed_candle:
            return

        # Orders fill at the live price (last trade of the bar in progress), not the old close.
        await self._evaluate_closed_bar(
            strat, cfg, symbol, timeframe, market_type, buffer, closed_candle, latest_candle.close, limit
        )

    async def _evaluate_closed_bar(
        self,
        strat: IStrategy,
        cfg: StrategyConfig,
        symbol: str,
        timeframe: str,
        market_type: MarketType,
        buffer: CandleBuffer,
        closed_candle: CandleBar,
        current_price: Decimal,
        limit: int,
    ) -> None:
        """Run the strategy once per closed bar and execute its signals.

        Both the stream and the poll loop can reach the same bar; the per-(strategy, symbol)
        lock plus the evaluated-bar set guarantee a single evaluation and a single order.
        """
        lock = self._eval_locks.setdefault((cfg.id, symbol), asyncio.Lock())
        async with lock:
            bar_key = (cfg.id, symbol, closed_candle.open_time.isoformat())
            if bar_key in self._evaluated_bars:
                return

            # Tell the strategy what it holds and hand it its benchmark candles before evaluating.
            if not await self._load_context_buffers(
                strat, symbol, buffer, timeframe, market_type, limit, closed_candle
            ):
                return  # retry on the next poll rather than skip this bar
            strat.position_state = await self._position_state_for(cfg)

            signals = strat.on_candle(closed_candle, buffer)
            self._evaluated_bars.add(bar_key)
            if len(self._evaluated_bars) > 1000:
                self._evaluated_bars = set(list(self._evaluated_bars)[-500:])

            if not signals:
                return

            logger.info(
                "[ContinuousStrategyRunner] Signal triggered for %s on %s closed at %s: %s",
                cfg.name, symbol, closed_candle.open_time, signals,
            )
            for sig in signals:
                await self._process_signal(sig, cfg, current_price)

    async def _position_state_for(self, cfg: StrategyConfig) -> dict[str, PositionSide]:
        if cfg.execution_mode != ExecutionMode.LIVE.value:
            return {
                p.symbol: p.side
                for p in self.paper_broker.positions
                if p.strategy_id == cfg.id and p.status == PositionStatus.OPEN
            }
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(Position).where(
                        Position.strategy_id == cfg.id,
                        Position.execution_mode == ExecutionMode.LIVE.value,
                        Position.status == PositionStatus.OPEN.value,
                    )
                )
            ).scalars().all()
        return {p.symbol: PositionSide(p.side) for p in rows}

    # ================================================================== stream ticks

    async def on_kline(self, event: MarketCandleEvent) -> None:
        """Handle a streamed kline: update buffers, drive SL/TP checks, run strategies on close."""
        key: KlineKey = (event.symbol, event.timeframe, event.market_type)
        buffer = self._get_buffer(event.symbol, event.timeframe, event.market_type)
        buffer.update_or_append(event)

        now = time.monotonic()
        if event.is_closed or now - self._last_tick_at.get(key, 0.0) >= TICK_THROTTLE_SEC:
            self._last_tick_at[key] = now
            await self.event_bus.publish(event)
            await self.paper_broker.on_price_update(
                symbol=event.symbol, high=event.high, low=event.low, close=event.close
            )
            await self._live_tick(event.symbol, event.close)

        if not event.is_closed:
            return
        closed_candle = buffer.latest_closed
        if closed_candle is None:
            return
        for cfg in self._active_configs:
            if (
                event.symbol in (cfg.symbols or [])
                and (cfg.timeframe or "15m") == event.timeframe
                and MarketType(cfg.market_type) == event.market_type
            ):
                strat = self._active_strategy_instances.get(cfg.id)
                if strat is None:
                    continue
                limit = max(50, strat.required_warmup_bars + 10)
                # Evaluate off the socket-reading path so one slow order never stalls the feed.
                task = asyncio.create_task(
                    self._evaluate_streamed_close(
                        strat, cfg, event.symbol, event.timeframe, event.market_type,
                        closed_candle, event.close, limit,
                    )
                )
                self._eval_tasks.add(task)
                task.add_done_callback(self._eval_tasks.discard)

    async def _evaluate_streamed_close(
        self,
        strat: IStrategy,
        cfg: StrategyConfig,
        symbol: str,
        timeframe: str,
        market_type: MarketType,
        closed_candle: CandleBar,
        current_price: Decimal,
        limit: int,
    ) -> None:
        try:
            buffer = self._get_buffer(symbol, timeframe, market_type, limit + 50)
            if not await self._ensure_warm(buffer, symbol, timeframe, market_type, limit):
                return
            await self._evaluate_closed_bar(
                strat, cfg, symbol, timeframe, market_type, buffer, closed_candle, current_price, limit
            )
        except Exception:
            logger.exception("Streamed evaluation failed for %s on %s", cfg.name, symbol)

    async def _live_tick(self, symbol: str, price: Decimal) -> None:
        """Feed a live price to Tier-2 protection (take-profit breakeven moves) for live positions."""
        if self.sl_tp_manager is None:
            return
        for state in self.sl_tp_manager.protections_for(symbol):
            conn_id = self._position_connection.get(state.position_id)
            adapter = self._live_adapters.get(conn_id) if conn_id else None
            if adapter is None:
                continue
            before = (state.stop_client_order_id, state.current_stop_price)
            try:
                await self.sl_tp_manager.on_market_tick(adapter, symbol, price)
            except Exception as exc:
                logger.warning("Tier-2 tick failed for %s: %s", symbol, exc)
                continue
            if (state.stop_client_order_id, state.current_stop_price) != before:
                await self._persist_moved_stop(state.position_id, before[0], state)
            # on_market_tick walks every protection on the symbol, so one call is enough.
            break

    async def _persist_moved_stop(self, position_id: str, old_client_id: str, state: Any) -> None:
        """Record a Tier-2 stop move so stop-fill sync and restarts track the new order."""
        async with self.session_factory() as session:
            pos = await session.get(Position, position_id)
            if pos is None:
                return
            old = (
                await session.execute(
                    select(Order).where(Order.position_id == position_id, Order.client_order_id == old_client_id)
                )
            ).scalar_one_or_none()
            if old is not None:
                old.status = OrderStatus.CANCELED.value
            pos.stop_loss_price = state.current_stop_price
            pos.active_stop_order_id = state.stop_client_order_id
            pos.stop_order_live = True
            session.add(
                Order(
                    id=str(uuid.uuid4()),
                    position_id=position_id,
                    client_order_id=state.stop_client_order_id,
                    exchange_order_id=state.exchange_stop_order_id or None,
                    symbol=pos.symbol,
                    market_type=pos.market_type,
                    purpose=OrderPurpose.HARD_STOP_LOSS.value,
                    order_type=OrderType.STOP_MARKET.value,
                    side=(OrderSide.SELL if pos.side == PositionSide.LONG.value else OrderSide.BUY).value,
                    reduce_only=True,
                    status=OrderStatus.OPEN.value,
                    requested_qty=pos.current_qty,
                    filled_qty=Decimal("0"),
                    stop_trigger_price=state.current_stop_price,
                )
            )
            await session.commit()

    # ================================================================== signals

    async def _process_signal(
        self,
        sig: TradeSignalEvent,
        cfg: StrategyConfig,
        current_price: Decimal,
    ) -> None:
        """Route a signal to the live exchange or the paper broker based on the strategy's mode."""
        if cfg.execution_mode == ExecutionMode.LIVE.value:
            await self._process_live_signal(sig, cfg, current_price)
        else:
            await self._process_paper_signal(sig, cfg, current_price)

    def _compute_quantity(
        self,
        sig: TradeSignalEvent,
        cfg: StrategyConfig,
        equity: Decimal,
        current_price: Decimal,
        rules: MarketRules,
        leverage: int,
    ) -> Decimal | None:
        """Order size shared by paper and live: target weight, fixed USD size, or risk-based."""
        risk_cfg = cfg.risk_config or {}
        max_notional_usd = to_decimal(risk_cfg.get("max_notional_usd", "5000.0"))
        base_size_usd = cfg.parameters.get("base_order_size_usd") if cfg.parameters else None
        is_dca_scale_in = isinstance(sig.metadata, dict) and sig.metadata.get("dca_order_type") == "SAFETY_SCALE_IN"

        if sig.target_weight is not None:
            # Volatility-targeted sizing: notional is a fraction of equity chosen by the strategy.
            # The notional cap applies only when the strategy's risk_config sets one explicitly.
            order_usd = equity * sig.target_weight
            if "max_notional_usd" in risk_cfg:
                order_usd = min(order_usd, max_notional_usd)
            qty = to_decimal(round(float(order_usd / current_price), 4))
            if qty <= Decimal("0"):
                logger.warning("Target-weight position size rounds to zero for %s", sig.symbol)
                return None
            return qty

        if base_size_usd:
            order_usd = to_decimal(base_size_usd)
            if is_dca_scale_in:
                step_num = int(sig.metadata.get("safety_step", 1))
                step_mult = to_decimal((cfg.parameters or {}).get("step_multiplier", "1.2"))
                order_usd = order_usd * (step_mult ** Decimal(str(step_num)))
            order_usd = min(order_usd, max_notional_usd)
            return to_decimal(round(float(order_usd / current_price), 4))

        # Standard fractional position sizing
        qty, err = PositionSizer.calculate_quantity(
            equity=equity,
            risk_per_trade_pct=Decimal("1.0"),
            entry_price=current_price,
            stop_loss_price=sig.stop_loss_price or (current_price * Decimal("0.95")),
            leverage=leverage,
            market_rules=rules,
        )
        if not qty or err:
            logger.warning("Position sizing failed for %s: %s", sig.symbol, err)
            return None

        # Enforce Max Notional Cap ($5,000 default) so tight stops never cause $30+ fee drag
        if qty * current_price > max_notional_usd:
            capped_qty = max_notional_usd / current_price
            qty = to_decimal(round(float(capped_qty), 3))
            logger.info(
                "Capped position size for %s to %s ($%.2f max notional cap to minimize fee drag).",
                sig.symbol,
                qty,
                max_notional_usd,
            )
        return qty

    # ------------------------------------------------------------------ live

    async def _get_live_adapter(self, connection_id: str | None) -> IExchangeAdapter | None:
        """Authenticated adapter for a connection, created once with clock sync and market rules loaded."""
        if not connection_id:
            return None
        if connection_id in self._live_adapters:
            return self._live_adapters[connection_id]
        async with self._live_lock:
            if connection_id in self._live_adapters:
                return self._live_adapters[connection_id]
            async with self.session_factory() as session:
                conn = await session.get(ExchangeConnection, connection_id)
                if conn is None or not conn.is_enabled:
                    return None
                adapter = self.adapter_factory.create_from_connection(conn)
                caps = {
                    MarketType.SPOT: conn.spot_capital_cap_usd,
                    MarketType.FUTURES_USDT: conn.futures_capital_cap_usd,
                }
            for mt in (MarketType.FUTURES_USDT, MarketType.SPOT):
                try:
                    await adapter.sync_server_time(mt)
                    break
                except Exception as exc:
                    logger.warning("Clock sync with %s failed (%s): %s", adapter.exchange_id.value, mt.value, exc)
            # Preload trading rules so the first live order does not pay for a large exchangeInfo download.
            for mt in (MarketType.FUTURES_USDT, MarketType.SPOT):
                try:
                    await adapter.load_markets(mt)
                except Exception as exc:
                    logger.warning("Market rules preload failed (%s): %s", mt.value, exc)
            self._live_adapters[connection_id] = adapter
            self._live_conn_caps[connection_id] = caps
            await self._restore_live_protections(connection_id)
            logger.info("Live adapter ready for connection %s (%s).", connection_id, adapter.exchange_id.value)
            return adapter

    async def _restore_live_protections(self, connection_id: str) -> None:
        if self.sl_tp_manager is None:
            return
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(Position).where(
                        Position.status == PositionStatus.OPEN.value,
                        Position.execution_mode == ExecutionMode.LIVE.value,
                        Position.exchange_connection_id == connection_id,
                    )
                )
            ).scalars().all()
        for pos in rows:
            self._position_connection[pos.id] = connection_id
            self.sl_tp_manager.restore_protection(pos)

    def _daily_start_equity(self, connection_id: str, market_type: MarketType, equity: Decimal) -> Decimal:
        today = datetime.now(timezone.utc).date()
        key = (connection_id, market_type)
        recorded = self._day_start_equity.get(key)
        if recorded is None or recorded[0] != today:
            self._day_start_equity[key] = (today, equity)
            return equity
        return recorded[1]

    async def _open_live_positions(self, connection_id: str) -> list[Position]:
        async with self.session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(Position).where(
                            Position.status == PositionStatus.OPEN.value,
                            Position.execution_mode == ExecutionMode.LIVE.value,
                            Position.exchange_connection_id == connection_id,
                        )
                    )
                ).scalars().all()
            )

    async def _process_live_signal(
        self,
        sig: TradeSignalEvent,
        cfg: StrategyConfig,
        current_price: Decimal,
    ) -> None:
        """Validate, size and place a real order on the strategy's exchange connection."""
        if self.live_broker is None:
            logger.error("Strategy %s is LIVE but no live broker is configured; signal dropped.", cfg.name)
            return
        conn_id = cfg.exchange_connection_id
        adapter = await self._get_live_adapter(conn_id)
        if adapter is None or conn_id is None:
            msg = f"LIVE strategy {cfg.name} has no enabled exchange connection; signal on {sig.symbol} dropped."
            logger.error(msg)
            await self.event_bus.publish(
                RiskAlertEvent(severity="CRITICAL", rule_name="LIVE_CONNECTION_MISSING", message=msg)
            )
            return

        market_type = MarketType(cfg.market_type)
        sig = sig.model_copy(update={"market_type": market_type, "execution_mode": ExecutionMode.LIVE})
        open_positions = await self._open_live_positions(conn_id)

        # Strategy-driven exit: flatten this strategy's live position on the symbol.
        if sig.action == SignalAction.EXIT:
            for pos in open_positions:
                if pos.strategy_id == sig.strategy_id and pos.symbol == sig.symbol and pos.side == sig.side.value:
                    logger.info(
                        "Closing LIVE position %s on %s (strategy exit: %s)",
                        pos.id[:8], sig.symbol, sig.metadata.get("exit_trigger", "STRATEGY_EXIT"),
                    )
                    await self.live_broker.close_position(adapter, pos.id, exit_reason="STRATEGY_EXIT")
            return

        if isinstance(sig.metadata, dict) and sig.metadata.get("dca_order_type") == "SAFETY_SCALE_IN":
            # Scaling in live needs the exchange stop resized to the new quantity; until that
            # exists, refuse rather than leave part of the position unprotected.
            logger.warning("DCA scale-in is not supported in LIVE mode yet; signal on %s skipped.", sig.symbol)
            return

        try:
            balance = await adapter.fetch_balance(market_type)
            rules = await adapter.get_market_rules(sig.symbol, market_type)
        except Exception as exc:
            logger.error("Could not read balance/market rules for live %s: %s", sig.symbol, exc)
            return

        cap = self._live_conn_caps.get(conn_id, {}).get(market_type) or Decimal("0")
        equity = balance.total_equity
        sizing_equity = min(equity, cap) if cap > Decimal("0") else equity
        risk_cfg = cfg.risk_config or {}
        leverage = int(risk_cfg.get("leverage", max(sig.leverage, 1))) if market_type == MarketType.FUTURES_USDT else 1
        sig = sig.model_copy(update={"leverage": leverage})

        approved, reject_reason = await self.risk_gatekeeper.evaluate_signal(
            signal=sig,
            open_positions_count=len(open_positions),
            current_equity=equity,
            daily_starting_equity=self._daily_start_equity(conn_id, market_type, equity),
            capital_cap_usd=cap if cap > Decimal("0") else None,
            existing_positions=open_positions,
            max_positions_per_symbol=1,
        )
        if not approved:
            logger.info("Live signal for %s rejected by Risk Gatekeeper: %s", sig.symbol, reject_reason)
            return

        qty = self._compute_quantity(sig, cfg, sizing_equity, current_price, rules, leverage)
        if qty is None:
            return
        qty = rules.format_quantity(qty)
        if qty < rules.min_qty or qty * current_price < rules.min_notional:
            logger.warning(
                "Live order for %s too small after exchange rounding (qty=%s, min_qty=%s, min_notional=%s).",
                sig.symbol, qty, rules.min_qty, rules.min_notional,
            )
            return

        if market_type == MarketType.FUTURES_USDT:
            config_key = (conn_id, sig.symbol, leverage)
            if config_key not in self._futures_configured:
                try:
                    await adapter.configure_futures_symbol(
                        symbol=sig.symbol, leverage=leverage, margin_mode=MarginMode.ISOLATED, one_way_mode=True
                    )
                    self._futures_configured.add(config_key)
                except Exception as exc:
                    logger.error("Futures setup for %s failed; live entry skipped: %s", sig.symbol, exc)
                    return

        logger.info(
            "Placing LIVE %s %s %s on %s (~%s)", sig.side.value, qty, sig.symbol, adapter.exchange_id.value, current_price
        )
        resp = await self.live_broker.execute_entry_signal(
            adapter=adapter, signal=sig, quantity=qty, exchange_connection_id=conn_id
        )
        if resp is not None:
            await self._restore_live_protections(conn_id)

    async def _sync_live_positions(self) -> None:
        """Detect exchange stops that fired since the last cycle and close those positions locally."""
        if self.live_broker is None:
            return
        async with self.session_factory() as session:
            conn_ids = {
                row
                for row in (
                    await session.execute(
                        select(Position.exchange_connection_id).where(
                            Position.status == PositionStatus.OPEN.value,
                            Position.execution_mode == ExecutionMode.LIVE.value,
                        )
                    )
                ).scalars().all()
                if row
            }
        for conn_id in conn_ids:
            adapter = await self._get_live_adapter(conn_id)
            if adapter is None:
                continue
            try:
                closed = await self.live_broker.sync_stop_fills(adapter, conn_id)
                if closed:
                    logger.info("Detected %d live stop-loss fill(s) on connection %s.", closed, conn_id)
            except Exception as exc:
                logger.warning("Stop fill sync failed for connection %s: %s", conn_id, exc)

    # ------------------------------------------------------------------ paper

    async def _process_paper_signal(
        self,
        sig: TradeSignalEvent,
        cfg: StrategyConfig,
        current_price: Decimal,
    ) -> None:
        """Run signal through Risk Gatekeeper and execute a paper trade."""
        # 0. Strategy-driven exit: close this strategy's open position on the symbol.
        if sig.action == SignalAction.EXIT:
            for pos in self.paper_broker.positions:
                if (
                    pos.strategy_id == sig.strategy_id
                    and pos.symbol == sig.symbol
                    and pos.side == sig.side
                    and pos.status == PositionStatus.OPEN
                ):
                    logger.info(
                        "ContinuousStrategyRunner closing paper position %s on %s (strategy exit: %s)",
                        pos.position_id[:8],
                        sig.symbol,
                        sig.metadata.get("exit_trigger", "STRATEGY_EXIT"),
                    )
                    await self.paper_broker.close_position_market(
                        pos.position_id,
                        exit_price=current_price,
                        exit_reason="STRATEGY_EXIT",
                    )
            return

        # 1. Gather all active positions from broker AND database for 100% accurate concentration checks
        active_positions = list(self.paper_broker.positions)
        if self.session_factory:
            try:
                async with self.session_factory() as session:
                    stmt = select(Position).where(Position.status == PositionStatus.OPEN.value)
                    db_open = (await session.execute(stmt)).scalars().all()
                    known_ids = {p.position_id for p in active_positions if hasattr(p, "position_id")}
                    for db_pos in db_open:
                        if db_pos.id not in known_ids:
                            active_positions.append(db_pos)
            except Exception as exc:
                logger.debug("Runner position sync check: %s", exc)

        # Gatekeeper validation with Concentration Guard (Max 1 position per symbol across account)
        approved, reject_reason = await self.risk_gatekeeper.evaluate_signal(
            signal=sig,
            open_positions_count=len(active_positions),
            current_equity=self.paper_broker.total_equity,
            daily_starting_equity=self.paper_broker.total_equity,
            existing_positions=active_positions,
            max_positions_per_symbol=1,
        )

        if not approved:
            logger.info("Signal for %s rejected by Risk Gatekeeper: %s", sig.symbol, reject_reason)
            return

        # 2. Determine position size
        base_size_usd = cfg.parameters.get("base_order_size_usd") if cfg.parameters else None
        is_dca_scale_in = isinstance(sig.metadata, dict) and sig.metadata.get("dca_order_type") == "SAFETY_SCALE_IN"
        dummy_rules = MarketRules(
            symbol=sig.symbol,
            exchange_symbol=sig.symbol.replace("/", ""),
            market_type=sig.market_type,
            base_asset=sig.symbol.split("/")[0],
            quote_asset="USDT",
            min_qty=Decimal("0.001"),
            max_qty=Decimal("1000"),
            step_size=Decimal("0.001"),
            tick_size=Decimal("0.01"),
            min_notional=Decimal("10.0"),
            max_leverage=10,
        )
        qty = self._compute_quantity(sig, cfg, self.paper_broker.total_equity, current_price, dummy_rules, leverage=2)
        if qty is None:
            return

        # 3. Check if scaling into an existing open DCA position
        existing_strat_pos = next(
            (
                p for p in self.paper_broker.positions
                if p.strategy_id == sig.strategy_id and p.symbol == sig.symbol
            ),
            None,
        )

        pos_id = existing_strat_pos.position_id if (is_dca_scale_in and existing_strat_pos) else str(uuid.uuid4())
        order_purpose = OrderPurpose.DCA_SAFETY if (is_dca_scale_in and existing_strat_pos) else OrderPurpose.ENTRY

        # Execute in paper broker
        side = OrderSide.BUY if sig.side == PositionSide.LONG else OrderSide.SELL
        order_req = NormalizedOrderRequest(
            client_order_id=f"cid-{sig.strategy_id[:8]}-{datetime.now(timezone.utc).strftime('%H%M%S')}",
            symbol=sig.symbol,
            market_type=sig.market_type,
            side=side,
            order_type=OrderType.MARKET,
            purpose=order_purpose,
            quantity=qty,
            stop_price=sig.stop_loss_price,
        )

        logger.info(
            "ContinuousStrategyRunner executing paper order (%s): %s %s %s @ %s",
            order_purpose.value,
            side.value,
            qty,
            sig.symbol,
            current_price,
        )
        resp = await self.paper_broker.submit_order(
            request=order_req,
            current_market_price=current_price,
            strategy_id=sig.strategy_id,
            position_id=pos_id,
            tp_targets=sig.take_profit_targets,
        )

        # 4. Also persist to SQLite database for persistent display in OrdersTable & PositionsTable
        try:
            slip = Decimal("0.0005") if side == OrderSide.BUY else -Decimal("0.0005")
            fill_price = current_price * (Decimal("1") + slip)
            fee = qty * fill_price * Decimal("0.0005")

            tp_state = [
                {
                    "target_index": t.target_index,
                    "price": str(t.price),
                    "close_dynamic_pct": str(t.close_dynamic_pct),
                    "move_sl_to_breakeven": t.move_sl_to_breakeven,
                }
                for t in (sig.take_profit_targets or [])
            ]

            async with self.session_factory() as session:
                if order_purpose == OrderPurpose.DCA_SAFETY:
                    db_pos = (await session.execute(select(Position).where(Position.id == pos_id))).scalar_one_or_none()
                    if db_pos:
                        old_cost = db_pos.entry_price * db_pos.current_qty
                        new_cost = fill_price * qty
                        new_total_qty = db_pos.current_qty + qty
                        db_pos.entry_price = (old_cost + new_cost) / new_total_qty
                        db_pos.current_qty = new_total_qty
                        db_pos.initial_qty += qty
                        db_pos.total_fees_usd = (db_pos.total_fees_usd or Decimal("0")) + fee
                        if tp_state:
                            db_pos.take_profit_state = tp_state
                else:
                    db_pos = Position(
                        id=pos_id,
                        strategy_id=sig.strategy_id,
                        execution_mode="PAPER",
                        symbol=sig.symbol,
                        market_type=sig.market_type.value,
                        side=sig.side.value,
                        margin_mode="ISOLATED",
                        leverage=1 if (base_size_usd or sig.target_weight is not None) else 2,
                        entry_price=fill_price,
                        initial_qty=qty,
                        current_qty=qty,
                        stop_loss_price=sig.stop_loss_price,
                        take_profit_state=tp_state,
                        stop_order_live=True,
                        unrealized_pnl_usd=Decimal("0"),
                        realized_pnl_usd=Decimal("0"),
                        total_fees_usd=fee,
                        status="OPEN",
                    )
                    session.add(db_pos)

                db_order = Order(
                    id=resp.exchange_order_id,
                    position_id=pos_id,
                    client_order_id=order_req.client_order_id,
                    exchange_order_id=resp.exchange_order_id,
                    symbol=sig.symbol,
                    market_type=sig.market_type.value,
                    purpose=order_purpose.value,
                    order_type="MARKET",
                    side=side.value,
                    status="FILLED",
                    requested_qty=qty,
                    filled_qty=qty,
                    avg_fill_price=fill_price,
                    fee_usd=fee,
                )
                session.add(db_order)
                await session.commit()
                logger.info("Persisted paper position %s and order %s (%s) to SQLite database.", pos_id, resp.exchange_order_id, order_purpose.value)
        except Exception as db_err:
            logger.warning("Failed to persist paper position to DB: %s", db_err)
