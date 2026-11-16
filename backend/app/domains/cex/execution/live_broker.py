"""Live Order Execution Broker with Database Persistence and Airbag Coordination."""

from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.decimal_utils import generate_client_order_id, to_decimal
from app.core.events import (
    EventBus,
    OrderLifecycleEvent,
    PositionUpdatedEvent,
    RiskAlertEvent,
    TradeSignalEvent,
)
from app.domains.cex.adapters.base import (
    IExchangeAdapter,
    NormalizedOrderRequest,
    NormalizedOrderResponse,
    OrderNotFoundError,
)
from app.domains.cex.execution.sl_tp_manager import HybridSlTpManager
from app.models.entities import Fill, Order, Position
from app.models.enums import (
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


class LiveExecutionBroker:
    """Coordinates live order placement, database persistence, and emergency SL protection."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_bus: EventBus,
        sl_tp_manager: HybridSlTpManager,
    ) -> None:
        self.session_factory = session_factory
        self.event_bus = event_bus
        self.sl_tp_manager = sl_tp_manager

    async def execute_entry_signal(
        self,
        adapter: IExchangeAdapter,
        signal: TradeSignalEvent,
        quantity: Decimal,
        exchange_connection_id: str,
        environment: ExecutionMode = ExecutionMode.LIVE,
    ) -> NormalizedOrderResponse | None:
        """Place an entry order on the exchange, record it, and place the exchange-side stop.

        `environment` (DEMO or LIVE) only labels the position; which Binance account the
        order reaches is decided by the adapter's keys and base URL.
        """
        order_side = OrderSide.BUY if signal.side == PositionSide.LONG else OrderSide.SELL
        cid = generate_client_order_id(
            purpose="ENTRY",
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            idempotency_seed=uuid.uuid4().hex[:8],
        )

        req = NormalizedOrderRequest(
            client_order_id=cid,
            symbol=signal.symbol,
            market_type=signal.market_type,
            side=order_side,
            order_type=OrderType.MARKET,
            purpose=OrderPurpose.ENTRY,
            quantity=quantity,
            stop_price=signal.stop_loss_price,
        )

        try:
            # 1. Submit order via exchange adapter (uses token rate limiter & idempotent retry)
            resp = await adapter.place_order(req)
        except Exception as exc:
            logger.error("Live order submission failed for %s: %s", signal.symbol, exc)
            await self.event_bus.publish(
                OrderLifecycleEvent(
                    order_id=str(uuid.uuid4()),
                    client_order_id=cid,
                    symbol=signal.symbol,
                    market_type=signal.market_type,
                    purpose=OrderPurpose.ENTRY,
                    order_type=OrderType.MARKET,
                    side=order_side,
                    status=OrderStatus.FAILED_SUBMIT,
                    requested_qty=quantity,
                    filled_qty=Decimal("0"),
                    reason=str(exc),
                )
            )
            return None

        # On a spot buy Binance charges the fee in the coin bought (unless paid in BNB), so the
        # wallet holds less than the filled quantity. Stops and exits must use what is held, or
        # they are rejected for insufficient balance.
        held_qty = resp.filled_qty or quantity
        if signal.market_type == MarketType.SPOT and order_side == OrderSide.BUY:
            base_asset = signal.symbol.split("/")[0].upper()
            base_fees = sum(
                (f.fee_amount for f in resp.fills if f.fee_currency.upper() == base_asset),
                Decimal("0"),
            )
            held_qty = held_qty - base_fees

        # 2. Persist Position, Order, and Fills in Database
        async with self.session_factory() as session:
            pos_id = str(uuid.uuid4())
            fill_price = resp.avg_fill_price or signal.signal_price

            pos = Position(
                id=pos_id,
                strategy_id=signal.strategy_id,
                exchange_connection_id=exchange_connection_id,
                execution_mode=environment.value,
                symbol=signal.symbol,
                market_type=signal.market_type.value,
                side=signal.side.value,
                margin_mode=MarginMode.ISOLATED.value,
                leverage=signal.leverage,
                entry_price=fill_price,
                initial_qty=held_qty,
                current_qty=held_qty,
                stop_loss_price=signal.stop_loss_price,
                stop_order_live=False,  # Updated after Tier-1 placement below
                status=PositionStatus.OPEN.value,
            )
            session.add(pos)

            ord_record = Order(
                id=str(uuid.uuid4()),
                position_id=pos_id,
                client_order_id=resp.client_order_id,
                exchange_order_id=resp.exchange_order_id,
                symbol=resp.symbol,
                market_type=resp.market_type.value,
                purpose=OrderPurpose.ENTRY.value,
                order_type=resp.order_type.value,
                side=resp.side.value,
                status=resp.status.value,
                requested_qty=resp.requested_qty,
                filled_qty=resp.filled_qty,
                avg_fill_price=resp.avg_fill_price,
                fee_usd=resp.fee_usd,
            )
            session.add(ord_record)

            for f in resp.fills:
                session.add(
                    Fill(
                        id=str(uuid.uuid4()),
                        order_id=ord_record.id,
                        exchange_trade_id=f.exchange_trade_id,
                        fill_price=f.fill_price,
                        fill_qty=f.fill_qty,
                        fee_amount=f.fee_amount,
                        fee_currency=f.fee_currency,
                    )
                )

            pos.take_profit_state = [
                {
                    "target_index": t.target_index,
                    "price": str(t.price),
                    "close_dynamic_pct": str(t.close_dynamic_pct),
                    "move_sl_to_breakeven": t.move_sl_to_breakeven,
                }
                for t in (signal.take_profit_targets or [])
            ]
            pos.total_fees_usd = resp.fee_usd or Decimal("0")
            await session.commit()

            # 3. Deploy immediate Tier-1 exchange-native stop-loss airbag
            if pos.current_qty > Decimal("0"):
                try:
                    stop_resp = await self.sl_tp_manager.place_initial_protection(
                        adapter=adapter,
                        position=pos,
                        tp_targets=signal.take_profit_targets,
                    )
                    pos.stop_order_live = True
                    pos.active_stop_order_id = stop_resp.client_order_id
                    session.add(
                        Order(
                            id=str(uuid.uuid4()),
                            position_id=pos_id,
                            client_order_id=stop_resp.client_order_id,
                            exchange_order_id=stop_resp.exchange_order_id,
                            symbol=pos.symbol,
                            market_type=pos.market_type,
                            purpose=OrderPurpose.HARD_STOP_LOSS.value,
                            order_type=stop_resp.order_type.value,
                            side=stop_resp.side.value,
                            reduce_only=True,
                            status=OrderStatus.OPEN.value,
                            requested_qty=pos.current_qty,
                            filled_qty=Decimal("0"),
                            stop_trigger_price=pos.stop_loss_price,
                        )
                    )
                    await session.commit()
                except Exception as exc:
                    # A live position with no exchange-side stop is exposed to any move while the
                    # engine is down or lagging. Flatten it rather than leave it naked.
                    logger.error(
                        "Failed to place Tier-1 hard stop for %s: %s. Closing the position.", pos.symbol, exc
                    )
                    await self._close_open_position(
                        session, adapter, pos, exit_reason="STOP_PLACEMENT_FAILED", cancel_stop=False
                    )
                    return resp

        # 4. Publish Position and Order events
        await self.event_bus.publish(
            PositionUpdatedEvent(
                position_id=pos_id,
                strategy_id=signal.strategy_id,
                symbol=signal.symbol,
                market_type=signal.market_type,
                execution_mode=environment,
                side=signal.side,
                status=PositionStatus.OPEN,
                current_qty=held_qty,
                entry_price=fill_price,
                stop_loss_price=signal.stop_loss_price,
                stop_order_live=pos.stop_order_live,
            )
        )
        return resp

    # ------------------------------------------------------------------ exits

    async def close_position(
        self,
        adapter: IExchangeAdapter,
        position_id: str,
        exit_reason: str = "STRATEGY_EXIT",
    ) -> bool:
        """Cancel the position's exchange stop and flatten it with a reduce-only market order."""
        async with self.session_factory() as session:
            pos = await session.get(Position, position_id)
            if pos is None or pos.status != PositionStatus.OPEN.value:
                return False
            return await self._close_open_position(session, adapter, pos, exit_reason=exit_reason)

    async def _close_open_position(
        self,
        session: AsyncSession,
        adapter: IExchangeAdapter,
        pos: Position,
        exit_reason: str,
        cancel_stop: bool = True,
    ) -> bool:
        market_type = MarketType(pos.market_type)
        side = PositionSide(pos.side)

        # Cancel the stop first so it cannot fire against an already-flat account.
        if cancel_stop and pos.active_stop_order_id:
            try:
                await adapter.cancel_stop_order(
                    symbol=pos.symbol,
                    market_type=market_type,
                    client_order_id=pos.active_stop_order_id,
                )
            except OrderNotFoundError:
                pass
            except Exception as exc:
                logger.warning("Could not cancel stop %s for %s: %s", pos.active_stop_order_id, pos.symbol, exc)

        cid = generate_client_order_id(
            purpose="MANUAL_CLOSE",
            strategy_id=pos.strategy_id or "manual",
            symbol=pos.symbol,
            idempotency_seed=f"close-{pos.id}",
        )
        try:
            resp = await adapter.close_position_market(
                symbol=pos.symbol,
                market_type=market_type,
                side=side,
                quantity=pos.current_qty,
                client_order_id=cid,
            )
        except Exception as exc:
            logger.error("Live close of %s (%s) failed: %s", pos.symbol, pos.id, exc)
            await self.event_bus.publish(
                RiskAlertEvent(
                    severity="CRITICAL",
                    rule_name="LIVE_CLOSE_FAILED",
                    message=f"Could not close live {pos.side} {pos.symbol} position {pos.id[:8]}: {exc}",
                )
            )
            return False

        exit_price = resp.avg_fill_price or pos.entry_price
        session.add(
            Order(
                id=str(uuid.uuid4()),
                position_id=pos.id,
                client_order_id=resp.client_order_id or cid,
                exchange_order_id=resp.exchange_order_id,
                symbol=pos.symbol,
                market_type=pos.market_type,
                purpose=OrderPurpose.MANUAL_CLOSE.value,
                order_type=OrderType.MARKET.value,
                side=resp.side.value,
                reduce_only=True,
                status=resp.status.value,
                requested_qty=pos.current_qty,
                filled_qty=resp.filled_qty,
                avg_fill_price=resp.avg_fill_price,
                fee_usd=resp.fee_usd,
            )
        )
        await self._mark_closed(session, pos, exit_price, resp.fee_usd or Decimal("0"), exit_reason)
        return True

    async def sync_stop_fills(self, adapter: IExchangeAdapter, exchange_connection_id: str) -> int:
        """Detect exchange-side stops that fired and close their positions in the database.

        The Tier-1 stop lives on the exchange, so when it fills nothing in the engine hears
        about it. Without this the position would stay OPEN locally forever, blocking new
        entries on the symbol and showing a phantom position in the dashboard.
        """
        closed = 0
        async with self.session_factory() as session:
            stmt = select(Position).where(
                Position.status == PositionStatus.OPEN.value,
                Position.exchange_connection_id == exchange_connection_id,
                Position.active_stop_order_id.is_not(None),
            )
            for pos in (await session.execute(stmt)).scalars().all():
                try:
                    stop = await adapter.fetch_stop_order(
                        symbol=pos.symbol,
                        market_type=MarketType(pos.market_type),
                        client_order_id=pos.active_stop_order_id,
                    )
                except OrderNotFoundError:
                    stop = None
                except Exception as exc:
                    logger.debug("Stop status check failed for %s: %s", pos.symbol, exc)
                    continue

                if stop is not None and stop.status == OrderStatus.FILLED:
                    await self._update_stop_order_row(session, pos, stop)
                    await self._mark_closed(
                        session,
                        pos,
                        stop.avg_fill_price or pos.stop_loss_price or pos.entry_price,
                        stop.fee_usd or Decimal("0"),
                        "HARD_STOP_LOSS",
                    )
                    closed += 1
                elif stop is None or stop.status in (
                    OrderStatus.CANCELED,
                    OrderStatus.EXPIRED,
                    OrderStatus.REJECTED,
                ):
                    if pos.stop_order_live:
                        pos.stop_order_live = False
                        await session.commit()
                        await self.event_bus.publish(
                            RiskAlertEvent(
                                severity="CRITICAL",
                                rule_name="STOP_LOSS_MISSING",
                                message=(
                                    f"Exchange stop for live {pos.side} {pos.symbol} position {pos.id[:8]} "
                                    f"is no longer active (status: {stop.status.value if stop else 'NOT_FOUND'})."
                                ),
                            )
                        )
        return closed

    async def _update_stop_order_row(
        self, session: AsyncSession, pos: Position, stop: NormalizedOrderResponse
    ) -> None:
        row = (
            await session.execute(
                select(Order).where(
                    Order.position_id == pos.id,
                    Order.client_order_id == pos.active_stop_order_id,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.status = OrderStatus.FILLED.value
            row.filled_qty = stop.filled_qty
            row.avg_fill_price = stop.avg_fill_price
            row.fee_usd = stop.fee_usd

    async def _mark_closed(
        self,
        session: AsyncSession,
        pos: Position,
        exit_price: Decimal,
        exit_fee: Decimal,
        exit_reason: str,
    ) -> None:
        direction = Decimal("1") if pos.side == PositionSide.LONG.value else Decimal("-1")
        gross = (exit_price - pos.entry_price) * pos.current_qty * direction
        fees = (pos.total_fees_usd or Decimal("0")) + exit_fee
        closed_qty = pos.current_qty

        pos.total_fees_usd = fees
        pos.realized_pnl_usd = (pos.realized_pnl_usd or Decimal("0")) + gross - fees
        pos.unrealized_pnl_usd = Decimal("0")
        pos.current_qty = Decimal("0")
        pos.stop_order_live = False
        pos.status = PositionStatus.CLOSED.value
        pos.exit_reason = exit_reason
        pos.closed_at = datetime.now(timezone.utc)
        await session.commit()
        self.sl_tp_manager.unregister(pos.id)

        logger.info(
            "Live position %s %s %s closed (%s) at %s, realized PnL %s",
            pos.id[:8], pos.side, pos.symbol, exit_reason, exit_price, pos.realized_pnl_usd,
        )
        await self.event_bus.publish(
            PositionUpdatedEvent(
                position_id=pos.id,
                strategy_id=pos.strategy_id,
                symbol=pos.symbol,
                market_type=MarketType(pos.market_type),
                execution_mode=ExecutionMode(pos.execution_mode),
                side=PositionSide(pos.side),
                status=PositionStatus.CLOSED,
                current_qty=Decimal("0"),
                entry_price=pos.entry_price,
                current_price=exit_price,
                stop_loss_price=pos.stop_loss_price,
                stop_order_live=False,
                realized_pnl_usd=pos.realized_pnl_usd,
                exit_reason=exit_reason,
            )
        )
