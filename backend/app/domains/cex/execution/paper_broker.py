"""Virtual Paper Trading Broker with In-Memory Matching Engine and PnL Tracking."""

from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any
import uuid

from app.core.decimal_utils import calculate_position_pnl, to_decimal
from app.core.events import (
    EventBus,
    OrderLifecycleEvent,
    PositionUpdatedEvent,
    TakeProfitTargetSpec,
)
from app.domains.cex.adapters.base import (
    CandleBar,
    NormalizedOrderFill,
    NormalizedOrderRequest,
    NormalizedOrderResponse,
    TickerSnapshot,
)
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
)

logger = logging.getLogger(__name__)


class PaperPosition(BaseModel := Any):
    """Active virtual paper position."""

    def __init__(
        self,
        position_id: str,
        strategy_id: str,
        symbol: str,
        market_type: MarketType,
        side: PositionSide,
        leverage: int,
        entry_price: Decimal,
        quantity: Decimal,
        stop_loss_price: Decimal,
        tp_targets: list[TakeProfitTargetSpec],
        created_at: datetime | None = None,
        status: PositionStatus = PositionStatus.OPEN,
    ) -> None:
        self.position_id = position_id
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.market_type = market_type
        self.side = side
        self.leverage = leverage
        self.entry_price = entry_price
        self.current_price = entry_price
        self.initial_qty = quantity
        self.current_qty = quantity
        self.stop_loss_price = stop_loss_price
        self.tp_targets = list(tp_targets)
        self.status = status
        self.stop_order_live = True
        self.unrealized_pnl = Decimal("0")
        self.realized_pnl = Decimal("0")
        self.created_at = created_at or datetime.now(timezone.utc)


class PaperTradingBroker:
    """Simulates realistic live execution with virtual wallet, fees, slippage, and SL/TP fills."""

    def __init__(
        self,
        event_bus: EventBus,
        initial_balance_usd: Decimal = Decimal("10000.00"),
        taker_fee: Decimal = Decimal("0.0005"),
        maker_fee: Decimal = Decimal("0.0002"),
        slippage_pct: Decimal = Decimal("0.0005"),  # 0.05% slippage on market orders
    ) -> None:
        self.event_bus = event_bus
        self.initial_balance_usd = initial_balance_usd
        self.available_balance = initial_balance_usd
        self.total_equity = initial_balance_usd
        self.taker_fee = taker_fee
        self.maker_fee = maker_fee
        self.slippage_pct = slippage_pct
        self._session_factory: Any = None

        # Active positions: position_id -> PaperPosition
        self._positions: dict[str, PaperPosition] = {}
        # Orders: order_id -> NormalizedOrderResponse
        self._orders: dict[str, NormalizedOrderResponse] = {}

    @property
    def open_positions_count(self) -> int:
        return len(self._positions)

    @property
    def positions(self) -> list[PaperPosition]:
        return list(self._positions.values())

    async def load_open_positions_from_db(self, session_factory: Any) -> int:
        """Hydrate in-memory positions and account equity from database on server startup."""
        from sqlalchemy import select
        from app.models.entities import Position
        self._session_factory = session_factory
        try:
            async with session_factory() as session:
                all_stmt = select(Position).where(Position.execution_mode == ExecutionMode.PAPER.value)
                all_positions = list((await session.execute(all_stmt)).scalars().all())

                net_realized = Decimal("0")
                open_entry_fees = Decimal("0")
                for p in all_positions:
                    if p.status == PositionStatus.CLOSED.value:
                        net_realized += (p.realized_pnl_usd or Decimal("0"))
                    elif p.status == PositionStatus.OPEN.value:
                        open_entry_fees += (p.total_fees_usd or Decimal("0"))

                self.available_balance = self.initial_balance_usd + net_realized - open_entry_fees
                self.total_equity = self.available_balance

                db_positions = [p for p in all_positions if p.status == PositionStatus.OPEN.value]
                for p in db_positions:
                    tp_targets: list[TakeProfitTargetSpec] = []
                    if p.take_profit_state:
                        for idx, t in enumerate(p.take_profit_state, start=1):
                            if isinstance(t, dict) and "price" in t:
                                tp_targets.append(
                                    TakeProfitTargetSpec(
                                        target_index=int(t.get("target_index", idx)),
                                        price=Decimal(str(t["price"])),
                                        close_dynamic_pct=Decimal(str(t.get("close_dynamic_pct", "100"))),
                                        move_sl_to_breakeven=bool(t.get("move_sl_to_breakeven", False)),
                                    )
                                )
                    if not tp_targets and p.entry_price:
                        tp_price = p.entry_price * Decimal("1.04") if p.side == PositionSide.LONG.value else p.entry_price * Decimal("0.96")
                        tp_targets.append(
                            TakeProfitTargetSpec(
                                target_index=1,
                                price=tp_price,
                                close_dynamic_pct=Decimal("100"),
                                move_sl_to_breakeven=False,
                            )
                        )
                    self._positions[p.id] = PaperPosition(
                        position_id=p.id,
                        strategy_id=p.strategy_id or "default",
                        symbol=p.symbol,
                        market_type=MarketType(p.market_type),
                        side=PositionSide(p.side),
                        leverage=p.leverage,
                        entry_price=p.entry_price,
                        quantity=p.current_qty,
                        stop_loss_price=p.stop_loss_price,
                        tp_targets=tp_targets,
                        created_at=p.opened_at,
                    )
                logger.info(
                    "Successfully hydrated %d active open positions from SQLite into broker (equity: $%.2f).",
                    len(self._positions),
                    self.total_equity,
                )
                return len(self._positions)
        except Exception as exc:
            logger.error("Failed to hydrate open positions from DB: %s", exc, exc_info=True)
            return len(self._positions)

    async def submit_order(
        self,
        request: NormalizedOrderRequest,
        current_market_price: Decimal,
        strategy_id: str = "strategy-default",
        position_id: str | None = None,
        tp_targets: list[TakeProfitTargetSpec] | None = None,
    ) -> NormalizedOrderResponse:
        """Execute a paper order against current simulated market price."""
        order_id = str(uuid.uuid4())

        # Simulate execution price with slippage
        if request.order_type == OrderType.MARKET:
            slip = self.slippage_pct if request.side == OrderSide.BUY else -self.slippage_pct
            fill_price = current_market_price * (Decimal("1") + slip)
        else:
            fill_price = request.price or current_market_price

        notional = request.quantity * fill_price
        fee = notional * self.taker_fee
        self.available_balance -= fee
        self.total_equity -= fee

        fill = NormalizedOrderFill(
            exchange_trade_id=f"paper-fill-{uuid.uuid4().hex[:8]}",
            fill_price=fill_price,
            fill_qty=request.quantity,
            fee_amount=fee,
            fee_currency="USDT",
        )

        resp = NormalizedOrderResponse(
            client_order_id=request.client_order_id,
            exchange_order_id=order_id,
            symbol=request.symbol,
            market_type=request.market_type,
            side=request.side,
            order_type=request.order_type,
            status=OrderStatus.FILLED,
            requested_qty=request.quantity,
            filled_qty=request.quantity,
            avg_fill_price=fill_price,
            price=request.price,
            stop_price=request.stop_price,
            fee_usd=fee,
            reduce_only=request.reduce_only,
            fills=[fill],
        )
        self._orders[order_id] = resp

        # Publish order lifecycle event
        await self.event_bus.publish(
            OrderLifecycleEvent(
                order_id=order_id,
                position_id=position_id,
                client_order_id=request.client_order_id,
                symbol=request.symbol,
                market_type=request.market_type,
                purpose=request.purpose,
                order_type=request.order_type,
                side=request.side,
                status=OrderStatus.FILLED,
                requested_qty=request.quantity,
                filled_qty=request.quantity,
                avg_fill_price=fill_price,
                fee_usd=fee,
            )
        )

        # Handle position opening or DCA safety scale-in
        if request.purpose in (OrderPurpose.ENTRY, OrderPurpose.DCA_SAFETY):
            existing_pos = self._positions.get(position_id) if position_id else None
            if existing_pos is not None and existing_pos.status == PositionStatus.OPEN:
                old_cost = existing_pos.entry_price * existing_pos.current_qty
                new_cost = fill_price * request.quantity
                new_qty = existing_pos.current_qty + request.quantity
                existing_pos.entry_price = (old_cost + new_cost) / new_qty
                existing_pos.current_price = fill_price
                existing_pos.current_qty = new_qty
                existing_pos.initial_qty += request.quantity
                if tp_targets:
                    existing_pos.tp_targets = list(tp_targets)
                pos = existing_pos
            else:
                pos_id = position_id or str(uuid.uuid4())
                pos_side = PositionSide.LONG if request.side == OrderSide.BUY else PositionSide.SHORT
                stop_price = request.stop_price or (
                    fill_price * Decimal("0.98") if pos_side == PositionSide.LONG else fill_price * Decimal("1.02")
                )

                pos = PaperPosition(
                    position_id=pos_id,
                    strategy_id=strategy_id,
                    symbol=request.symbol,
                    market_type=request.market_type,
                    side=pos_side,
                    leverage=1,
                    entry_price=fill_price,
                    quantity=request.quantity,
                    stop_loss_price=stop_price,
                    tp_targets=tp_targets or [],
                )
                self._positions[pos_id] = pos

            await self.event_bus.publish(
                PositionUpdatedEvent(
                    position_id=pos.position_id,
                    strategy_id=pos.strategy_id,
                    symbol=pos.symbol,
                    market_type=pos.market_type,
                    execution_mode=ExecutionMode.PAPER,
                    side=pos.side,
                    status=PositionStatus.OPEN,
                    current_price=pos.current_price,
                    current_qty=pos.current_qty,
                    entry_price=pos.entry_price,
                    stop_loss_price=pos.stop_loss_price,
                    stop_order_live=True,
                )
            )

        return resp

    async def on_price_update(
        self,
        symbol: str,
        high: Decimal,
        low: Decimal,
        close: Decimal,
    ) -> None:
        """Evaluate Stop-Loss and Take-Profit triggers on active paper positions."""
        closed_position_ids: list[str] = []

        for pos_id, pos in list(self._positions.items()):
            if pos.symbol != symbol or pos.status != PositionStatus.OPEN:
                continue

            exit_triggered = False
            exit_price = Decimal("0")
            exit_reason = ""

            # Check Stop-Loss
            if pos.side == PositionSide.LONG and low <= pos.stop_loss_price:
                exit_price = pos.stop_loss_price * (Decimal("1") - self.slippage_pct)
                exit_reason = "HARD_STOP_LOSS"
                exit_triggered = True
            elif pos.side == PositionSide.SHORT and high >= pos.stop_loss_price:
                exit_price = pos.stop_loss_price * (Decimal("1") + self.slippage_pct)
                exit_reason = "HARD_STOP_LOSS"
                exit_triggered = True

            # Check Take-Profit targets if SL not hit
            if not exit_triggered and pos.tp_targets:
                tp = pos.tp_targets[0]
                if pos.side == PositionSide.LONG and high >= tp.price:
                    exit_price = tp.price
                    exit_reason = f"TAKE_PROFIT_{tp.target_index}"
                    exit_triggered = True
                elif pos.side == PositionSide.SHORT and low <= tp.price:
                    exit_price = tp.price
                    exit_reason = f"TAKE_PROFIT_{tp.target_index}"
                    exit_triggered = True

            if exit_triggered:
                await self._execute_paper_exit(pos, exit_price, exit_reason)
                closed_position_ids.append(pos_id)
            else:
                # Update live mark price and unrealized PnL for active open position
                gross_unrealized, _ = calculate_position_pnl(
                    side=pos.side.value,
                    entry_price=pos.entry_price,
                    current_or_exit_price=close,
                    quantity=pos.current_qty,
                    total_fees_usd=Decimal("0"),
                )
                pos.current_price = close
                pos.unrealized_pnl = gross_unrealized

                # Pre-compute unrealized PnL % for display (leverage-adjusted)
                notional = pos.entry_price * pos.current_qty
                pnl_pct = float(gross_unrealized / notional * 100 * pos.leverage) if notional > 0 else 0.0

                if self._session_factory is not None:
                    try:
                        from sqlalchemy import select
                        from app.models.entities import Position
                        async with self._session_factory() as session:
                            db_pos = (
                                await session.execute(select(Position).where(Position.id == pos.position_id))
                            ).scalar_one_or_none()
                            if db_pos and db_pos.status == PositionStatus.OPEN.value:
                                db_pos.unrealized_pnl_usd = gross_unrealized
                                await session.commit()
                    except Exception as db_err:
                        logger.debug("Failed to update live unrealized PnL in DB: %s", db_err)

                await self.event_bus.publish(
                    PositionUpdatedEvent(
                        position_id=pos.position_id,
                        strategy_id=pos.strategy_id,
                        symbol=pos.symbol,
                        market_type=pos.market_type,
                        execution_mode=ExecutionMode.PAPER,
                        side=pos.side,
                        status=PositionStatus.OPEN,
                        current_price=close,
                        current_qty=pos.current_qty,
                        entry_price=pos.entry_price,
                        stop_loss_price=pos.stop_loss_price,
                        stop_order_live=pos.stop_order_live,
                        unrealized_pnl_usd=gross_unrealized,
                        unrealized_pnl_pct=pnl_pct,
                    )
                )

        for pid in closed_position_ids:
            self._positions.pop(pid, None)

    async def close_position_market(
        self,
        position_id: str,
        exit_price: Decimal | None = None,
        exit_reason: str = "MANUAL_CLOSE",
    ) -> bool:
        """Manually close an open paper position at current mark/entry price and persist to DB."""
        pos = self._positions.get(position_id)
        if not pos or pos.status != PositionStatus.OPEN:
            return False

        raw_price = exit_price or pos.current_price or pos.entry_price
        fill_price = (
            raw_price * (Decimal("1") - self.slippage_pct)
            if pos.side == PositionSide.LONG
            else raw_price * (Decimal("1") + self.slippage_pct)
        )
        await self._execute_paper_exit(pos, fill_price, exit_reason)
        self._positions.pop(position_id, None)
        return True

    async def _execute_paper_exit(
        self,
        pos: PaperPosition,
        exit_price: Decimal,
        exit_reason: str,
    ) -> None:
        """Finalize paper position exit, update equity, emit events, and persist to SQLite."""
        exit_notional = pos.current_qty * exit_price
        exit_fee = exit_notional * self.taker_fee
        gross_pnl, net_pnl = calculate_position_pnl(
            side=pos.side.value,
            entry_price=pos.entry_price,
            current_or_exit_price=exit_price,
            quantity=pos.current_qty,
            total_fees_usd=exit_fee,
        )

        self.available_balance += net_pnl
        self.total_equity += net_pnl
        pos.realized_pnl += net_pnl
        pos.current_price = exit_price
        pos.unrealized_pnl = Decimal("0")
        pos.status = PositionStatus.CLOSED

        exit_side = OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY
        exit_purpose = (
            OrderPurpose.HARD_STOP_LOSS
            if "STOP" in exit_reason
            else (OrderPurpose.TAKE_PROFIT if "PROFIT" in exit_reason else OrderPurpose.MANUAL_CLOSE)
        )
        exit_order_id = str(uuid.uuid4())
        exit_cid = f"exit-{pos.position_id[:8]}-{datetime.now(timezone.utc).strftime('%H%M%S')}"

        logger.info(
            "[PaperTradingBroker] Closed position %s (%s %s) via %s @ %s | Net PnL: $%.2f | Equity: $%.2f",
            pos.position_id[:8],
            pos.side.value,
            pos.symbol,
            exit_reason,
            exit_price,
            net_pnl,
            self.total_equity,
        )

        if self._session_factory is not None:
            try:
                from sqlalchemy import select
                from app.models.entities import Order, Position
                async with self._session_factory() as session:
                    db_pos = (
                        await session.execute(select(Position).where(Position.id == pos.position_id))
                    ).scalar_one_or_none()
                    if db_pos:
                        entry_fee = db_pos.total_fees_usd or Decimal("0")
                        total_fees = entry_fee + exit_fee
                        _, full_net_pnl = calculate_position_pnl(
                            side=pos.side.value,
                            entry_price=pos.entry_price,
                            current_or_exit_price=exit_price,
                            quantity=pos.initial_qty,
                            total_fees_usd=total_fees,
                        )
                        db_pos.status = PositionStatus.CLOSED.value
                        db_pos.current_qty = Decimal("0")
                        db_pos.unrealized_pnl_usd = Decimal("0")
                        db_pos.realized_pnl_usd = full_net_pnl
                        db_pos.total_fees_usd = total_fees
                        db_pos.exit_reason = exit_reason
                        db_pos.stop_order_live = False
                        db_pos.closed_at = datetime.now(timezone.utc)

                    db_exit_order = Order(
                        id=exit_order_id,
                        position_id=pos.position_id,
                        client_order_id=exit_cid,
                        exchange_order_id=exit_order_id,
                        symbol=pos.symbol,
                        market_type=pos.market_type.value,
                        purpose=exit_purpose.value,
                        order_type=OrderType.MARKET.value,
                        side=exit_side.value,
                        status=OrderStatus.FILLED.value,
                        requested_qty=pos.initial_qty,
                        filled_qty=pos.initial_qty,
                        avg_fill_price=exit_price,
                        fee_usd=exit_fee,
                        reduce_only=True,
                    )
                    session.add(db_exit_order)
                    await session.commit()
            except Exception as db_err:
                logger.warning("Failed to persist paper position exit to DB: %s", db_err)

        await self.event_bus.publish(
            OrderLifecycleEvent(
                order_id=exit_order_id,
                position_id=pos.position_id,
                client_order_id=exit_cid,
                symbol=pos.symbol,
                market_type=pos.market_type,
                purpose=exit_purpose,
                order_type=OrderType.MARKET,
                side=exit_side,
                status=OrderStatus.FILLED,
                requested_qty=pos.initial_qty,
                filled_qty=pos.initial_qty,
                avg_fill_price=exit_price,
                fee_usd=exit_fee,
            )
        )

        await self.event_bus.publish(
            PositionUpdatedEvent(
                position_id=pos.position_id,
                strategy_id=pos.strategy_id,
                symbol=pos.symbol,
                market_type=pos.market_type,
                execution_mode=ExecutionMode.PAPER,
                side=pos.side,
                status=PositionStatus.CLOSED,
                current_qty=Decimal("0"),
                entry_price=pos.entry_price,
                realized_pnl_usd=net_pnl,
                exit_reason=exit_reason,
            )
        )
