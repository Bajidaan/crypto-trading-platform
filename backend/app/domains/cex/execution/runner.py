"""Continuous Background Strategy Runner and Real-Time Market Feeder."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.decimal_utils import to_decimal
from app.core.events import (
    EventBus,
    MarketCandleEvent,
    OrderLifecycleEvent,
    PositionUpdatedEvent,
    TradeSignalEvent,
)
from app.domains.cex.adapters.base import MarketRules, NormalizedOrderRequest
from app.domains.cex.adapters.binance import BinanceAdapter
from app.domains.cex.adapters.factory import ExchangeAdapterFactory
from app.domains.cex.execution.paper_broker import PaperTradingBroker
from app.domains.cex.market_data.buffer import CandleBuffer
from app.domains.cex.risk.gatekeeper import RiskGatekeeper
from app.domains.cex.risk.position_sizer import PositionSizer
from app.domains.cex.strategies.base import IStrategy
from app.domains.cex.strategies.registry import StrategyRegistry
from app.models.entities import ExchangeConnection, StrategyConfig
from app.models.enums import (
    ExecutionMode,
    MarketType,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
)

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.session_factory = session_factory
        self.event_bus = event_bus
        self.paper_broker = paper_broker
        self.risk_gatekeeper = risk_gatekeeper
        self.adapter_factory = adapter_factory
        self.poll_interval = poll_interval_seconds

        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._buffers: dict[tuple[str, str, str], CandleBuffer] = {}
        self._active_strategy_instances: dict[str, IStrategy] = {}
        self._evaluated_bars: set[tuple[str, str, str]] = set()
        self._default_adapter: BinanceAdapter | None = None
        self.event_bus.subscribe(PositionUpdatedEvent, self._handle_position_closed)

    def start(self) -> None:
        """Start the background strategy evaluation loop."""
        if self._running:
            return
        self._running = True
        self._default_adapter = BinanceAdapter(api_key="", api_secret="", is_testnet=False)
        self._task = asyncio.create_task(self._run_loop())
        logger.info("ContinuousStrategyRunner started (polling interval: %ss).", self.poll_interval)

    async def stop(self) -> None:
        """Gracefully terminate background runner loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._default_adapter:
            await self._default_adapter.close()
            self._default_adapter = None
        logger.info("ContinuousStrategyRunner stopped.")

    async def _handle_position_closed(self, event: PositionUpdatedEvent) -> None:
        """Notify strategy instances when a position is closed so stateful strategies (e.g. Smart DCA) reset."""
        from app.models.enums import PositionStatus
        if event.status == PositionStatus.CLOSED:
            for strat in self._active_strategy_instances.values():
                if hasattr(strat, "on_position_event"):
                    try:
                        strat.on_position_event(event)
                    except Exception as exc:
                        logger.debug("Strategy on_position_event callback error: %s", exc)

    def _get_buffer(self, symbol: str, timeframe: str, market_type: MarketType) -> CandleBuffer:
        key = (symbol, timeframe, market_type.value)
        if key not in self._buffers:
            self._buffers[key] = CandleBuffer(
                symbol=symbol,
                timeframe=timeframe,
                market_type=market_type,
                max_size=300,
            )
        return self._buffers[key]

    async def _run_loop(self) -> None:
        """Periodic evaluation loop."""
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
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    async def _evaluate_active_strategies(self) -> None:
        """Fetch active strategies from database and evaluate incoming candles."""
        async with self.session_factory() as session:
            stmt = select(StrategyConfig).where(StrategyConfig.is_active == True)
            active_configs = list((await session.execute(stmt)).scalars().all())

        if not active_configs:
            return

        for cfg in active_configs:
            try:
                # Maintain or update strategy instance
                strat = self._active_strategy_instances.get(cfg.id)
                if not strat or strat.parameters != cfg.parameters or strat.timeframe != cfg.timeframe:
                    strat = StrategyRegistry.create_instance(cfg)
                    self._active_strategy_instances[cfg.id] = strat

                symbols = cfg.symbols or ["BTC/USDT"]
                market_type = MarketType(cfg.market_type)
                timeframe = cfg.timeframe or "15m"

                for sym in symbols:
                    await self._evaluate_symbol_strategy(
                        strat=strat,
                        cfg=cfg,
                        symbol=sym,
                        timeframe=timeframe,
                        market_type=market_type,
                    )
            except Exception as exc:
                logger.warning("Strategy evaluation failed for %s (%s): %s", cfg.name, cfg.id, exc)

    async def _evaluate_symbol_strategy(
        self,
        strat: IStrategy,
        cfg: StrategyConfig,
        symbol: str,
        timeframe: str,
        market_type: MarketType,
    ) -> None:
        """Fetch latest candles for symbol, update buffer, evaluate SL/TP, and process signals."""
        adapter = self._default_adapter
        if not adapter:
            return

        buffer = self._get_buffer(symbol, timeframe, market_type)

        # 1. Fetch recent candles from exchange (50 bars for indicator warmup)
        limit = max(50, strat.required_warmup_bars + 10)
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

        # 4. Evaluate strategy on latest CLOSED candle (candles[-1] is still in progress)
        closed_candle = next((c for c in reversed(candles) if c.is_closed), None)
        if not closed_candle:
            return

        bar_key = (cfg.id, symbol, closed_candle.open_time.isoformat())
        if bar_key in self._evaluated_bars:
            return

        signals = strat.on_candle(closed_candle, buffer)
        self._evaluated_bars.add(bar_key)
        if len(self._evaluated_bars) > 1000:
            self._evaluated_bars = set(list(self._evaluated_bars)[-500:])

        if not signals:
            return

        logger.info("[ContinuousStrategyRunner] Signal triggered for %s on %s closed at %s: %s", cfg.name, symbol, closed_candle.open_time, signals)

        # 5. Process signals
        for sig in signals:
            await self._process_signal(sig, cfg, closed_candle.close)

    async def _process_signal(
        self,
        sig: TradeSignalEvent,
        cfg: StrategyConfig,
        current_price: Decimal,
    ) -> None:
        """Run signal through Risk Gatekeeper and execute paper/live trade."""
        # 1. Gather all active positions from broker AND database for 100% accurate concentration checks
        active_positions = list(self.paper_broker.positions)
        if self.session_factory:
            try:
                async with self.session_factory() as session:
                    from sqlalchemy import select
                    from app.models.entities import Position
                    from app.models.enums import PositionStatus
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

        # 2. Determine position size with $5,000 max notional cap to prevent excessive fee drag
        max_notional_usd = to_decimal((cfg.risk_config or {}).get("max_notional_usd", "5000.0"))
        base_size_usd = cfg.parameters.get("base_order_size_usd") if cfg.parameters else None
        is_dca_scale_in = isinstance(sig.metadata, dict) and sig.metadata.get("dca_order_type") == "SAFETY_SCALE_IN"

        if base_size_usd:
            order_usd = to_decimal(base_size_usd)
            if is_dca_scale_in:
                step_num = int(sig.metadata.get("safety_step", 1))
                step_mult = to_decimal((cfg.parameters or {}).get("step_multiplier", "1.2"))
                order_usd = order_usd * (step_mult ** Decimal(str(step_num)))
            order_usd = min(order_usd, max_notional_usd)
            raw_qty = order_usd / current_price
            qty = to_decimal(round(float(raw_qty), 4))
        else:
            # Standard fractional position sizing
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
            qty, err = PositionSizer.calculate_quantity(
                equity=self.paper_broker.total_equity,
                risk_per_trade_pct=Decimal("1.0"),
                entry_price=current_price,
                stop_loss_price=sig.stop_loss_price or (current_price * Decimal("0.95")),
                leverage=2,
                market_rules=dummy_rules,
            )
            if not qty or err:
                logger.warning("Position sizing failed for %s: %s", sig.symbol, err)
                return

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

        # 3. Check if scaling into an existing open DCA position
        existing_strat_pos = next(
            (
                p for p in self.paper_broker.positions
                if p.strategy_id == sig.strategy_id and p.symbol == sig.symbol
            ),
            None,
        )

        import uuid
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
            from app.models.entities import Fill, Order, Position
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
                        leverage=1 if base_size_usd else 2,
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
