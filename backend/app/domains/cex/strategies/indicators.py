"""Technical Indicator calculations using exact Decimal arithmetic and robust smoothing."""

from decimal import Decimal
import math

from app.core.decimal_utils import to_decimal


def calculate_ema(series: list[Decimal], period: int) -> list[Decimal]:
    """Calculate Exponential Moving Average (EMA) series."""
    if not series or period <= 0:
        return []
    if len(series) < period:
        # Not enough data: return empty or partial SMA
        return []

    multiplier = Decimal("2") / Decimal(str(period + 1))
    ema_series: list[Decimal] = []

    # Initialize with simple moving average of first `period` items
    initial_sma = sum(series[:period]) / Decimal(str(period))
    ema_series.append(initial_sma)

    current_ema = initial_sma
    for val in series[period:]:
        current_ema = (val - current_ema) * multiplier + current_ema
        ema_series.append(current_ema)

    return ema_series


def calculate_rsi(series: list[Decimal], period: int = 14) -> list[Decimal]:
    """Calculate Wilder's Relative Strength Index (RSI) series."""
    if len(series) <= period:
        return []

    gains: list[Decimal] = []
    losses: list[Decimal] = []

    for i in range(1, len(series)):
        change = series[i] - series[i - 1]
        if change > Decimal("0"):
            gains.append(change)
            losses.append(Decimal("0"))
        else:
            gains.append(Decimal("0"))
            losses.append(abs(change))

    # Initial average gain and loss
    avg_gain = sum(gains[:period]) / Decimal(str(period))
    avg_loss = sum(losses[:period]) / Decimal(str(period))

    rsi_series: list[Decimal] = []

    def _compute_rsi(ag: Decimal, al: Decimal) -> Decimal:
        if al == Decimal("0"):
            return Decimal("100")
        rs = ag / al
        return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))

    rsi_series.append(_compute_rsi(avg_gain, avg_loss))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * Decimal(str(period - 1)) + gains[i]) / Decimal(str(period))
        avg_loss = (avg_loss * Decimal(str(period - 1)) + losses[i]) / Decimal(str(period))
        rsi_series.append(_compute_rsi(avg_gain, avg_loss))

    return rsi_series


def calculate_atr(
    highs: list[Decimal],
    lows: list[Decimal],
    closes: list[Decimal],
    period: int = 14,
) -> list[Decimal]:
    """Calculate Average True Range (ATR) series using Wilder's smoothing."""
    length = min(len(highs), len(lows), len(closes))
    if length <= period:
        return []

    true_ranges: list[Decimal] = []
    for i in range(1, length):
        h = highs[i]
        l = lows[i]
        prev_c = closes[i - 1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return []

    initial_atr = sum(true_ranges[:period]) / Decimal(str(period))
    atr_series: list[Decimal] = [initial_atr]

    current_atr = initial_atr
    for tr in true_ranges[period:]:
        current_atr = (current_atr * Decimal(str(period - 1)) + tr) / Decimal(str(period))
        atr_series.append(current_atr)

    return atr_series


def calculate_bollinger_bands(
    closes: list[Decimal],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[Decimal, Decimal, Decimal]:
    """Calculate latest Bollinger Bands (Upper, Middle, Lower).

    Returns:
        (upper_band, middle_band, lower_band)
    """
    if len(closes) < period:
        raise ValueError(f"Need at least {period} close prices for Bollinger Bands")

    window = closes[-period:]
    middle = sum(window) / Decimal(str(period))

    variance = sum((x - middle) ** 2 for x in window) / Decimal(str(period))
    std = Decimal(str(math.sqrt(float(variance))))
    num_std_dec = Decimal(str(num_std))

    upper = middle + (std * num_std_dec)
    lower = middle - (std * num_std_dec)
    return upper, middle, lower


def calculate_adx(
    highs: list[Decimal],
    lows: list[Decimal],
    closes: list[Decimal],
    period: int = 14,
) -> Decimal:
    """Calculate the latest Average Directional Index (ADX) value."""
    length = min(len(highs), len(lows), len(closes))
    if length < period * 2:
        return Decimal("20.0")  # Default neutral reading if not enough bars

    tr_list: list[Decimal] = []
    plus_dm: list[Decimal] = []
    minus_dm: list[Decimal] = []

    for i in range(1, length):
        h = highs[i]
        l = lows[i]
        prev_h = highs[i - 1]
        prev_l = lows[i - 1]
        prev_c = closes[i - 1]

        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)

        up_move = h - prev_h
        down_move = prev_l - l

        if up_move > down_move and up_move > Decimal("0"):
            plus_dm.append(up_move)
        else:
            plus_dm.append(Decimal("0"))

        if down_move > up_move and down_move > Decimal("0"):
            minus_dm.append(down_move)
        else:
            minus_dm.append(Decimal("0"))

    # Initial smoothed sums
    smooth_tr = sum(tr_list[:period])
    smooth_pdm = sum(plus_dm[:period])
    smooth_mdm = sum(minus_dm[:period])

    dx_list: list[Decimal] = []

    def _calc_dx(pdm: Decimal, mdm: Decimal, tr_val: Decimal) -> Decimal:
        if tr_val == Decimal("0"):
            return Decimal("0")
        pdi = (pdm / tr_val) * Decimal("100")
        mdi = (mdm / tr_val) * Decimal("100")
        diff = abs(pdi - mdi)
        total = pdi + mdi
        if total == Decimal("0"):
            return Decimal("0")
        return (diff / total) * Decimal("100")

    dx_list.append(_calc_dx(smooth_pdm, smooth_mdm, smooth_tr))

    for i in range(period, len(tr_list)):
        smooth_tr = smooth_tr - (smooth_tr / Decimal(str(period))) + tr_list[i]
        smooth_pdm = smooth_pdm - (smooth_pdm / Decimal(str(period))) + plus_dm[i]
        smooth_mdm = smooth_mdm - (smooth_mdm / Decimal(str(period))) + minus_dm[i]
        dx_list.append(_calc_dx(smooth_pdm, smooth_mdm, smooth_tr))

    if len(dx_list) < period:
        return dx_list[-1] if dx_list else Decimal("20.0")

    # Final ADX is smoothed DX
    initial_adx = sum(dx_list[:period]) / Decimal(str(period))
    current_adx = initial_adx
    for dx in dx_list[period:]:
        current_adx = (current_adx * Decimal(str(period - 1)) + dx) / Decimal(str(period))

    return current_adx
