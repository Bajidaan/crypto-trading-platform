"""Risk Gatekeeper for Pre-Trade Validation and Global Circuit Breakers."""

from decimal import Decimal
import logging
from typing import Any

from app.core.events import EventBus, RiskAlertEvent, TradeSignalEvent
from app.models.entities import GlobalRiskState

logger = logging.getLogger(__name__)


class RiskGatekeeper:
    """Enforces global circuit breakers, position caps, and maximum daily drawdown limits."""

    def __init__(
        self,
        event_bus: EventBus,
        risk_state: GlobalRiskState | None = None,
    ) -> None:
        self.event_bus = event_bus
        self._risk_state = risk_state or GlobalRiskState(
            id=1,
            is_paused=False,
            kill_switch_active=False,
            max_concurrent_positions=5,
            max_daily_drawdown_pct=Decimal("5.0"),
            max_leverage_cap=10,
            default_risk_per_trade_pct=Decimal("1.0"),
            compounding_enabled=False,
        )

    @property
    def risk_state(self) -> GlobalRiskState:
        return self._risk_state

    def update_risk_state(self, new_state: GlobalRiskState) -> None:
        self._risk_state = new_state

    async def evaluate_signal(
        self,
        signal: TradeSignalEvent,
        open_positions_count: int,
        current_equity: Decimal,
        daily_starting_equity: Decimal,
        capital_cap_usd: Decimal | None = None,
        existing_positions: list[Any] | None = None,
        max_positions_per_symbol: int = 1,
    ) -> tuple[bool, str | None]:
        """Validate a trade signal against all active circuit breakers.

        Returns:
            (True, None) if approved.
            (False, reason_str) if rejected.
        """
        # 1. Kill Switch Check (Most critical)
        if self._risk_state.kill_switch_active:
            reason = "Emergency Kill Switch is ACTIVE. All new trading signals rejected."
            logger.warning("[Risk Gatekeeper] Rejected: %s", reason)
            return False, reason

        # 2. Global Trading Pause Check
        if self._risk_state.is_paused:
            reason = "Trading engine is currently PAUSED. New entries rejected."
            logger.info("[Risk Gatekeeper] Rejected: %s", reason)
            return False, reason

        # 3. Maximum Concurrent Positions Check
        max_positions = self._risk_state.max_concurrent_positions
        if open_positions_count >= max_positions:
            reason = (
                f"Concurrent open positions limit reached ({open_positions_count}/{max_positions}). "
                f"Cannot open new position for {signal.symbol}."
            )
            logger.warning("[Risk Gatekeeper] Rejected: %s", reason)
            await self.event_bus.publish(
                RiskAlertEvent(
                    severity="WARNING",
                    rule_name="MAX_CONCURRENT_POSITIONS",
                    message=reason,
                )
            )
            return False, reason

        # 4. Single Position Per Symbol / Concentration Guard
        if existing_positions:
            is_dca_scale_in = (
                isinstance(signal.metadata, dict)
                and signal.metadata.get("dca_order_type") == "SAFETY_SCALE_IN"
            )
            # Check same strategy already holding this symbol
            same_strat_pos = [
                p for p in existing_positions
                if getattr(p, "strategy_id", None) == signal.strategy_id
                and getattr(p, "symbol", None) == signal.symbol
            ]
            if same_strat_pos and not is_dca_scale_in:
                reason = (
                    f"Strategy already holds an active open position on {signal.symbol}. "
                    f"Duplicate entry blocked to prevent overexposure."
                )
                logger.info("[Risk Gatekeeper] Concentration Guard: %s", reason)
                await self.event_bus.publish(
                    RiskAlertEvent(
                        severity="INFO",
                        rule_name="CONCENTRATION_GUARD",
                        message=reason,
                    )
                )
                return False, reason

            # Check aggregate symbol concentration across all strategies (unless scaling into own DCA position)
            same_sym_pos = [
                p for p in existing_positions
                if getattr(p, "symbol", None) == signal.symbol
            ]
            if len(same_sym_pos) >= max_positions_per_symbol and not (is_dca_scale_in and same_strat_pos):
                reason = (
                    f"Symbol {signal.symbol} reached maximum account concentration ({len(same_sym_pos)}/{max_positions_per_symbol}). "
                    f"New entry blocked to preserve portfolio diversification."
                )
                logger.info("[Risk Gatekeeper] Concentration Guard: %s", reason)
                await self.event_bus.publish(
                    RiskAlertEvent(
                        severity="INFO",
                        rule_name="CONCENTRATION_GUARD",
                        message=reason,
                    )
                )
                return False, reason

        # 4. Maximum Daily Drawdown Circuit Breaker
        if daily_starting_equity > Decimal("0"):
            drawdown_usd = daily_starting_equity - current_equity
            drawdown_pct = (drawdown_usd / daily_starting_equity) * Decimal("100")
            max_dd = self._risk_state.max_daily_drawdown_pct

            if drawdown_pct >= max_dd:
                reason = (
                    f"Maximum daily drawdown circuit breaker triggered! "
                    f"Current drawdown is {drawdown_pct:.2f}% (Limit: {max_dd:.2f}%). "
                    f"Trading paused automatically."
                )
                logger.error("[Risk Gatekeeper] CRITICAL: %s", reason)
                self._risk_state.is_paused = True
                await self.event_bus.publish(
                    RiskAlertEvent(
                        severity="CRITICAL",
                        rule_name="MAX_DAILY_DRAWDOWN",
                        message=reason,
                    )
                )
                return False, reason

        # 5. Leverage Cap Check
        max_lev = self._risk_state.max_leverage_cap
        if signal.leverage > max_lev:
            reason = (
                f"Requested leverage {signal.leverage}x exceeds system maximum leverage cap of {max_lev}x."
            )
            logger.warning("[Risk Gatekeeper] Rejected: %s", reason)
            return False, reason

        # 6. Capital Cap Check (Exchange Connection budget)
        if capital_cap_usd is not None and capital_cap_usd > Decimal("0"):
            if current_equity > capital_cap_usd * Decimal("1.5"):
                logger.info(
                    "[Risk Gatekeeper] Current equity (%s) exceeds connection capital cap (%s)",
                    current_equity,
                    capital_cap_usd,
                )

        return True, None
