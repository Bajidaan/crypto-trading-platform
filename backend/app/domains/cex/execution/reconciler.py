"""Startup and Periodic Orderbook Reconciler & Orphan Position Detector."""

from decimal import Decimal
import logging
from typing import Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.decimal_utils import generate_client_order_id, to_decimal
from app.core.events import EventBus, RiskAlertEvent
from app.domains.cex.adapters.base import (
    ExchangePositionSnapshot,
    IExchangeAdapter,
    NormalizedOrderResponse,
    OrderNotFoundError,
)
from app.models.entities import Order, Position
from app.models.enums import MarketType, OrderSide, OrderStatus, PositionSide, PositionStatus

logger = logging.getLogger(__name__)


class ReconciliationReport:
    """Structured report of adjustments made during an exchange reconciliation cycle."""

    def __init__(self) -> None:
        self.reconciled_orders_count: int = 0
        self.stale_positions_closed_count: int = 0
        self.orphan_positions_detected: list[str] = []
        self.emergency_stops_placed_count: int = 0
        self.errors: list[str] = []


class StartupReconciler:
    """Enforces absolute synchronization between local database state and exchange reality."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_bus: EventBus,
    ) -> None:
        self.session_factory = session_factory
        self.event_bus = event_bus

    async def reconcile(
        self,
        adapter: IExchangeAdapter,
        market_type: MarketType = MarketType.FUTURES_USDT,
    ) -> ReconciliationReport:
        """Run full reconciliation cycle against the target exchange."""
        report = ReconciliationReport()
        logger.info("Starting order and position reconciliation for %s (%s)...", adapter.exchange_id.value, market_type.value)

        try:
            # 1. Fetch live open orders and positions from exchange
            exch_open_orders = await adapter.fetch_open_orders(symbol=None, market_type=market_type)
            exch_positions = await adapter.fetch_open_positions(market_type=market_type)
        except Exception as exc:
            err_msg = f"Failed to query exchange during reconciliation: {exc}"
            logger.error(err_msg)
            report.errors.append(err_msg)
            return report

        exch_orders_by_cid = {o.client_order_id: o for o in exch_open_orders if o.client_order_id}
        exch_orders_by_eid = {o.exchange_order_id: o for o in exch_open_orders if o.exchange_order_id}
        exch_positions_by_symbol = {p.symbol: p for p in exch_positions}

        async with self.session_factory() as session:
            # 2. Reconcile Open Database Orders
            db_orders_stmt = select(Order).where(
                Order.status.in_([OrderStatus.SUBMITTED.value, OrderStatus.PENDING_SUBMIT.value])
            )
            db_orders = (await session.execute(db_orders_stmt)).scalars().all()

            for ord_row in db_orders:
                # Check if still active on exchange
                matched_exch_order = exch_orders_by_cid.get(
                    ord_row.client_order_id
                ) or exch_orders_by_eid.get(ord_row.exchange_order_id or "")

                if matched_exch_order:
                    if matched_exch_order.status != OrderStatus(ord_row.status):
                        ord_row.status = matched_exch_order.status.value
                        ord_row.filled_qty = matched_exch_order.filled_qty
                        if matched_exch_order.avg_fill_price:
                            ord_row.avg_fill_price = matched_exch_order.avg_fill_price
                        report.reconciled_orders_count += 1
                else:
                    # Order is no longer open on exchange; query final historical status
                    try:
                        hist_order = await adapter.fetch_order(
                            symbol=ord_row.symbol,
                            market_type=market_type,
                            exchange_order_id=ord_row.exchange_order_id,
                            client_order_id=ord_row.client_order_id,
                        )
                        ord_row.status = hist_order.status.value
                        ord_row.filled_qty = hist_order.filled_qty
                        if hist_order.avg_fill_price:
                            ord_row.avg_fill_price = hist_order.avg_fill_price
                        report.reconciled_orders_count += 1
                    except OrderNotFoundError:
                        ord_row.status = OrderStatus.CANCELED.value
                        report.reconciled_orders_count += 1

            # 3. Reconcile Open Database Positions
            db_positions_stmt = select(Position).where(
                Position.status == PositionStatus.OPEN.value,
                Position.market_type == market_type.value,
            )
            db_positions = (await session.execute(db_positions_stmt)).scalars().all()

            for pos_row in db_positions:
                exch_pos = exch_positions_by_symbol.get(pos_row.symbol)
                if exch_pos is None or exch_pos.quantity == Decimal("0"):
                    # Position was closed while engine was offline (e.g. SL or manual exit)
                    pos_row.status = PositionStatus.RECONCILED_STALE.value
                    pos_row.current_qty = Decimal("0")
                    report.stale_positions_closed_count += 1
                    logger.warning(
                        "Marked stale DB position %s (%s) as RECONCILED_STALE (closed on exchange)",
                        pos_row.id,
                        pos_row.symbol,
                    )
                else:
                    # Update mark price and quantity
                    pos_row.current_qty = exch_pos.quantity
                    pos_row.unrealized_pnl_usd = exch_pos.unrealized_pnl

                    # 4. Mandatory Safety Airbag Check: Ensure active exchange-side stop exists
                    has_active_stop = any(
                        o.symbol == pos_row.symbol and "STOP" in o.order_type.value
                        for o in exch_open_orders
                    )
                    if not has_active_stop:
                        logger.warning(
                            "Open position %s (%s) is MISSING exchange-side stop-loss! Placing emergency stop...",
                            pos_row.id,
                            pos_row.symbol,
                        )
                        stop_side = OrderSide.SELL if pos_row.side == PositionSide.LONG.value else OrderSide.BUY
                        emergency_cid = generate_client_order_id(
                            purpose="HARD_STOP_LOSS",
                            strategy_id=pos_row.strategy_id,
                            symbol=pos_row.symbol,
                            idempotency_seed="reconcile-emerg",
                        )
                        try:
                            await adapter.place_hard_stop_loss(
                                symbol=pos_row.symbol,
                                market_type=market_type,
                                side=stop_side,
                                quantity=pos_row.current_qty,
                                stop_price=pos_row.stop_loss_price,
                                client_order_id=emergency_cid,
                            )
                            pos_row.stop_order_live = True
                            report.emergency_stops_placed_count += 1
                        except Exception as exc:
                            err = f"Failed to place emergency stop for {pos_row.symbol}: {exc}"
                            logger.error(err)
                            report.errors.append(err)

            # 5. Detect Orphan Exchange Positions (present on exchange but absent from DB)
            db_symbols = {p.symbol for p in db_positions if p.status == PositionStatus.OPEN.value}
            for exch_pos in exch_positions:
                if exch_pos.symbol not in db_symbols:
                    report.orphan_positions_detected.append(exch_pos.symbol)
                    alert_msg = f"Detected ORPHAN position on {adapter.exchange_id.value}: {exch_pos.symbol} (Qty={exch_pos.quantity})"
                    logger.error(alert_msg)
                    await self.event_bus.publish(
                        RiskAlertEvent(
                            severity="CRITICAL",
                            rule_name="ORPHAN_POSITION_DETECTED",
                            message=alert_msg,
                        )
                    )

            await session.commit()

        logger.info(
            "Reconciliation finished: %d orders updated, %d stale positions closed, %d emergency stops placed.",
            report.reconciled_orders_count,
            report.stale_positions_closed_count,
            report.emergency_stops_placed_count,
        )
        return report
