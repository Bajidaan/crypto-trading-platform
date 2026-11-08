"""Two-Tier Hybrid Stop-Loss and Dynamic Take-Profit Manager."""

from decimal import Decimal
import logging
from typing import Any

from app.core.decimal_utils import generate_client_order_id, to_decimal
from app.core.events import EventBus, PositionUpdatedEvent, TakeProfitTargetSpec
from app.domains.cex.adapters.base import IExchangeAdapter, NormalizedOrderResponse
from app.models.entities import Position
from app.models.enums import MarketType, OrderSide, PositionSide, PositionStatus

logger = logging.getLogger(__name__)


class ManagedProtectionState:
    """In-memory active state of dynamic protection for a single open position."""

    def __init__(
        self,
        position_id: str,
        strategy_id: str,
        symbol: str,
        market_type: MarketType,
        side: PositionSide,
        entry_price: Decimal,
        quantity: Decimal,
        current_stop_price: Decimal,
        exchange_stop_order_id: str,
        stop_client_order_id: str,
        tp_targets: list[TakeProfitTargetSpec] | None = None,
        trailing_pct: Decimal | None = None,
    ) -> None:
        self.position_id = position_id
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.market_type = market_type
        self.side = side
        self.entry_price = entry_price
        self.quantity = quantity
        self.current_stop_price = current_stop_price
        self.exchange_stop_order_id = exchange_stop_order_id
        self.stop_client_order_id = stop_client_order_id
        self.tp_targets = list(tp_targets or [])
        self.trailing_pct = trailing_pct
        self.highest_price_seen = entry_price if side == PositionSide.LONG else Decimal("0")
        self.lowest_price_seen = entry_price if side == PositionSide.SHORT else Decimal("1000000000")
        self.breakeven_triggered = False


class HybridSlTpManager:
    """Orchestrates Tier-1 matching-engine hard stops and Tier-2 engine-managed dynamic TP/trailing exits."""

    def __init__(
        self,
        event_bus: EventBus,
    ) -> None:
        self.event_bus = event_bus
        # position_id -> ManagedProtectionState
        self._protections: dict[str, ManagedProtectionState] = {}

    async def place_initial_protection(
        self,
        adapter: IExchangeAdapter,
        position: Position,
        tp_targets: list[TakeProfitTargetSpec] | None = None,
        trailing_pct: Decimal | None = None,
    ) -> NormalizedOrderResponse:
        """Place immediate Tier-1 exchange-native hard stop-loss and register for Tier-2 dynamic exits."""
        stop_side = OrderSide.SELL if position.side == PositionSide.LONG.value else OrderSide.BUY
        cid = generate_client_order_id(
            purpose="HARD_STOP_LOSS",
            strategy_id=position.strategy_id,
            symbol=position.symbol,
            idempotency_seed=position.id[:8],
        )

        resp = await adapter.place_hard_stop_loss(
            symbol=position.symbol,
            market_type=MarketType(position.market_type),
            side=stop_side,
            quantity=position.current_qty,
            stop_price=position.stop_loss_price,
            client_order_id=cid,
        )

        # Register in-memory dynamic protection state
        self._protections[position.id] = ManagedProtectionState(
            position_id=position.id,
            strategy_id=position.strategy_id,
            symbol=position.symbol,
            market_type=MarketType(position.market_type),
            side=PositionSide(position.side),
            entry_price=position.entry_price,
            quantity=position.current_qty,
            current_stop_price=position.stop_loss_price,
            exchange_stop_order_id=resp.exchange_order_id,
            stop_client_order_id=cid,
            tp_targets=tp_targets,
            trailing_pct=trailing_pct,
        )

        logger.info(
            "Registered Tier-1 protection for %s (pos_id=%s, stop_price=%s, order_id=%s)",
            position.symbol,
            position.id,
            position.stop_loss_price,
            resp.exchange_order_id,
        )
        return resp

    async def on_market_tick(
        self,
        adapter: IExchangeAdapter,
        symbol: str,
        current_price: Decimal,
    ) -> list[str]:
        """Evaluate Tier-2 dynamic exit conditions (Take-Profit scale-outs and Breakeven moves)."""
        actions_taken: list[str] = []

        for pos_id, state in list(self._protections.items()):
            if state.symbol != symbol:
                continue

            # 1. Update high/low watermarks
            if state.side == PositionSide.LONG and current_price > state.highest_price_seen:
                state.highest_price_seen = current_price
            elif state.side == PositionSide.SHORT and current_price < state.lowest_price_seen:
                state.lowest_price_seen = current_price

            # 2. Check Take-Profit targets and Breakeven trigger
            if state.tp_targets and not state.breakeven_triggered:
                target = state.tp_targets[0]
                tp_hit = (
                    (state.side == PositionSide.LONG and current_price >= target.price)
                    or (state.side == PositionSide.SHORT and current_price <= target.price)
                )
                if tp_hit:
                    # Trigger breakeven stop move if configured
                    if target.move_sl_to_breakeven:
                        new_stop = state.entry_price
                        new_cid = generate_client_order_id(
                            purpose="HARD_STOP_LOSS",
                            strategy_id=state.strategy_id,
                            symbol=state.symbol,
                            idempotency_seed=f"be-{pos_id[:6]}",
                        )
                        try:
                            stop_side = OrderSide.SELL if state.side == PositionSide.LONG else OrderSide.BUY
                            amended = await adapter.amend_or_replace_stop_loss(
                                existing_order_id=state.exchange_stop_order_id,
                                existing_client_order_id=state.stop_client_order_id,
                                symbol=state.symbol,
                                market_type=state.market_type,
                                side=stop_side,
                                quantity=state.quantity,
                                new_stop_price=new_stop,
                                new_client_order_id=new_cid,
                            )
                            state.current_stop_price = new_stop
                            state.exchange_stop_order_id = amended.exchange_order_id
                            state.stop_client_order_id = new_cid
                            state.breakeven_triggered = True
                            action_msg = f"Moved stop-loss to BREAKEVEN ({new_stop}) for {state.symbol}"
                            actions_taken.append(action_msg)
                            logger.info(action_msg)
                        except Exception as exc:
                            logger.error("Failed to move stop to breakeven for %s: %s", state.symbol, exc)

        return actions_taken

    def unregister(self, position_id: str) -> None:
        """Remove protection tracking when position closes."""
        self._protections.pop(position_id, None)

    def get_state(self, position_id: str) -> ManagedProtectionState | None:
        return self._protections.get(position_id)
