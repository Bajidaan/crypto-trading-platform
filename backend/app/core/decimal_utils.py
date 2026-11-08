"""Fixed-point Decimal utilities, exchange filter quantizers, and deterministic ID generation."""

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
import hashlib
import re

CLIENT_ORDER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,36}$")


def to_decimal(value: Decimal | int | float | str) -> Decimal:
    """Safely convert a numeric value to Decimal without binary float precision artifacts."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, str)):
        try:
            return Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise ValueError(f"Invalid decimal value: {value!r}") from exc
    if isinstance(value, float):
        # Convert float via str to avoid IEEE-754 representation noise
        return Decimal(str(value))
    raise TypeError(f"Unsupported type for Decimal conversion: {type(value).__name__}")


def quantize_price(
    price: Decimal | int | float | str,
    tick_size: Decimal | int | float | str,
    rounding: str = ROUND_HALF_UP,
) -> Decimal:
    """Align a price to an exact multiple of the exchange's tick_size.

    Supports both base-10 tick sizes (e.g., 0.01, 0.0001) and non-base-10 tick sizes (e.g., 0.05, 0.5).
    """
    p = to_decimal(price)
    tick = to_decimal(tick_size)
    if tick <= Decimal("0"):
        raise ValueError(f"tick_size must be positive, got {tick}")
    if p < Decimal("0"):
        raise ValueError(f"price must be non-negative, got {p}")

    steps = (p / tick).to_integral_value(rounding=rounding)
    quantized = (steps * tick).quantize(tick.normalize(), rounding=rounding)
    return quantized


def quantize_quantity(
    quantity: Decimal | int | float | str,
    lot_step: Decimal | int | float | str,
) -> Decimal:
    """Align an order quantity DOWN to an exact multiple of the exchange's lot_step.

    Always uses ROUND_DOWN so the order never exceeds available balance or margin.
    """
    qty = to_decimal(quantity)
    step = to_decimal(lot_step)
    if step <= Decimal("0"):
        raise ValueError(f"lot_step must be positive, got {step}")
    if qty < Decimal("0"):
        raise ValueError(f"quantity must be non-negative, got {qty}")

    steps = (qty / step).to_integral_value(rounding=ROUND_DOWN)
    quantized = (steps * step).quantize(step.normalize(), rounding=ROUND_DOWN)
    return quantized


def validate_order_filters(
    price: Decimal | int | float | str,
    quantity: Decimal | int | float | str,
    min_qty: Decimal | int | float | str,
    min_notional: Decimal | int | float | str,
) -> tuple[bool, str | None]:
    """Validate quantized price and quantity against exchange min_qty and min_notional rules."""
    p = to_decimal(price)
    q = to_decimal(quantity)
    mq = to_decimal(min_qty)
    mn = to_decimal(min_notional)

    if q <= Decimal("0"):
        return False, "Order quantity is zero or negative after lot_step rounding."
    if q < mq:
        return False, f"Order quantity {q} is below exchange minimum quantity {mq}."
    notional = p * q
    if notional < mn:
        return False, f"Order notional {notional:.4f} is below exchange minimum notional {mn}."
    return True, None


def calculate_position_pnl(
    side: str,
    entry_price: Decimal | int | float | str,
    current_or_exit_price: Decimal | int | float | str,
    quantity: Decimal | int | float | str,
    total_fees_usd: Decimal | int | float | str = Decimal("0"),
) -> tuple[Decimal, Decimal]:
    """Calculate (gross_pnl_usd, net_pnl_usd) for a LONG or SHORT position."""
    entry = to_decimal(entry_price)
    exit_p = to_decimal(current_or_exit_price)
    qty = to_decimal(quantity)
    fees = to_decimal(total_fees_usd)

    normalized_side = side.upper().strip()
    if normalized_side == "LONG":
        gross_pnl = (exit_p - entry) * qty
    elif normalized_side == "SHORT":
        gross_pnl = (entry - exit_p) * qty
    else:
        raise ValueError(f"Unsupported position side: {side!r}. Expected 'LONG' or 'SHORT'.")

    net_pnl = gross_pnl - fees
    return gross_pnl, net_pnl


def generate_client_order_id(
    purpose: str,
    strategy_id: str,
    symbol: str,
    idempotency_seed: str,
) -> str:
    """Generate a deterministic, exchange-compliant clientOrderId (<= 36 chars, alphanumeric/dash/underscore).

    Given the exact same inputs, this function always returns the exact same clientOrderId,
    preventing duplicate orders across network retries.
    """
    clean_symbol = re.sub(r"[^a-zA-Z0-9]", "", symbol.upper())[:6]
    clean_purpose = re.sub(r"[^a-zA-Z0-9]", "", purpose.upper())[:3]
    payload = f"{purpose}:{strategy_id}:{symbol}:{idempotency_seed}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:20]
    candidate = f"th_{clean_purpose}_{clean_symbol}_{digest}"[:36]
    if not CLIENT_ORDER_ID_PATTERN.match(candidate):
        raise ValueError(f"Generated invalid clientOrderId: {candidate}")
    return candidate
