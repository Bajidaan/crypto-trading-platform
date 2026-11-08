"""Fixed-Fractional Position Sizer with Exchange Precision and Filter Validation."""

from decimal import Decimal
import logging

from app.core.decimal_utils import to_decimal
from app.domains.cex.adapters.base import MarketRules

logger = logging.getLogger(__name__)


class PositionSizer:
    """Calculates safe, quantized position order quantities enforcing risk % and exchange filters."""

    @staticmethod
    def calculate_quantity(
        equity: Decimal,
        risk_per_trade_pct: Decimal,
        entry_price: Decimal,
        stop_loss_price: Decimal,
        leverage: int,
        market_rules: MarketRules,
        max_leverage_cap: int = 10,
    ) -> tuple[Decimal | None, str | None]:
        """Compute the quantized order quantity based on fixed account equity risk.

        Returns:
            (quantized_quantity, None) if successful and meets all filters.
            (None, error_message) if parameters are invalid or order fails filters.
        """
        if equity <= Decimal("0"):
            return None, "Account equity must be positive to size position."
        if entry_price <= Decimal("0") or stop_loss_price <= Decimal("0"):
            return None, "Entry price and stop-loss price must be strictly positive."

        risk_distance = abs(entry_price - stop_loss_price)
        if risk_distance == Decimal("0"):
            return None, "Stop-loss price cannot equal entry price."

        effective_leverage = max(1, min(int(leverage), max_leverage_cap, market_rules.max_leverage))
        cash_risk = equity * (risk_per_trade_pct / Decimal("100"))

        # Quantity strictly determined by cash risk divided by distance to stop loss
        raw_qty = cash_risk / risk_distance

        # Hard upper bound: cannot exceed account equity multiplied by effective leverage
        max_qty_by_margin = (equity * Decimal(str(effective_leverage))) / entry_price
        capped_qty = min(raw_qty, max_qty_by_margin)

        # Quantize down to symbol's exact stepSize
        quantized_qty = market_rules.format_quantity(capped_qty)

        # Validate against exchange filters (minQty, minNotional)
        if quantized_qty < market_rules.min_qty:
            return None, (
                f"Calculated quantity {quantized_qty} is below exchange minimum quantity "
                f"{market_rules.min_qty} for {market_rules.symbol}."
            )

        is_valid, filter_err = market_rules.validate_order(
            price=entry_price, quantity=quantized_qty
        )
        if not is_valid:
            return None, filter_err

        return quantized_qty, None
