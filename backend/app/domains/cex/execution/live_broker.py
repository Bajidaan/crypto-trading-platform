"""Live Order Execution Broker with Database Persistence and Airbag Coordination."""

from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any
import uuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.decimal_utils import generate_client_order_id, to_decimal
from app.core.events import EventBus, OrderLifecycleEvent, PositionUpdatedEvent, TradeSignalEvent
from app.domains.cex.adapters.base import (
    IExchangeAdapter,
    NormalizedOrderRequest,
    NormalizedOrderResponse,
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
    ) -> NormalizedOrderResponse | None:
        """Place live entry order, record to DB, and initialize exchange-side hard stop-loss."""
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

        # 2. Persist Position, Order, and Fills in Database
        async with self.session_factory() as session:
            pos_id = str(uuid.uuid4())
            fill_price = resp.avg_fill_price or signal.signal_price

            pos = Position(
                id=pos_id,
                strategy_id=signal.strategy_id,
                exchange_connection_id=exchange_connection_id,
                execution_mode=ExecutionMode.LIVE.value,
                symbol=signal.symbol,
                market_type=signal.market_type.value,
                side=signal.side.value,
                margin_mode=MarginMode.ISOLATED.value,
                leverage=signal.leverage,
                entry_price=fill_price,
                initial_qty=resp.filled_qty or quantity,
                current_qty=resp.filled_qty or quantity,
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

            await session.commit()

            # 3. Deploy immediate Tier-1 exchange-native stop-loss airbag
            if pos.current_qty > Decimal("0"):
                try:
                    await self.sl_tp_manager.place_initial_protection(
                        adapter=adapter,
                        position=pos,
                        tp_targets=signal.take_profit_targets,
                    )
                    pos.stop_order_live = True
                    await session.commit()
                except Exception as exc:
                    logger.error("Failed to place Tier-1 hard stop for %s: %s", pos.symbol, exc)

        # 4. Publish Position and Order events
        await self.event_bus.publish(
            PositionUpdatedEvent(
                position_id=pos_id,
                strategy_id=signal.strategy_id,
                symbol=signal.symbol,
                market_type=signal.market_type,
                execution_mode=ExecutionMode.LIVE,
                side=signal.side,
                status=PositionStatus.OPEN,
                current_qty=resp.filled_qty or quantity,
                entry_price=fill_price,
                stop_loss_price=signal.stop_loss_price,
                stop_order_live=pos.stop_order_live,
            )
        )
        return resp
