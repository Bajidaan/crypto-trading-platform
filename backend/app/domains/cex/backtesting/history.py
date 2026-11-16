"""Paginated historical candle download for backtests longer than one exchange request."""

import time

from app.domains.cex.adapters.base import CandleBar, IExchangeAdapter
from app.domains.cex.market_data.buffer import timeframe_to_seconds
from app.models.enums import MarketType

MAX_HISTORY_BARS = 20000


async def fetch_ohlcv_history(
    adapter: IExchangeAdapter,
    symbol: str,
    market_type: MarketType,
    timeframe: str,
    total_bars: int,
    page_size: int = 1000,
    now_ms: int | None = None,
) -> list[CandleBar]:
    """Fetch the most recent `total_bars` CLOSED candles, paging forward from the oldest.

    Returns fewer bars when the symbol has not been listed that long. The bar still in
    progress is dropped so a backtest never trades on an unfinished candle.
    """
    total_bars = max(1, min(total_bars, MAX_HISTORY_BARS))
    tf_ms = timeframe_to_seconds(timeframe) * 1000
    end_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    cursor_ms = end_ms - (total_bars + 1) * tf_ms

    by_open_time: dict[int, CandleBar] = {}
    while cursor_ms < end_ms:
        page = await adapter.fetch_ohlcv(
            symbol=symbol,
            market_type=market_type,
            timeframe=timeframe,
            since_ms=cursor_ms,
            limit=page_size,
        )
        if not page:
            break
        for bar in page:
            by_open_time[int(bar.open_time.timestamp() * 1000)] = bar
        newest_ms = max(int(bar.open_time.timestamp() * 1000) for bar in page)
        next_cursor = newest_ms + tf_ms
        if next_cursor <= cursor_ms:
            break  # no forward progress: stop instead of looping forever
        cursor_ms = next_cursor
        if len(page) < page_size:
            break

    bars = [
        bar
        for open_ms, bar in sorted(by_open_time.items())
        if bar.is_closed and open_ms + tf_ms <= end_ms
    ]
    return bars[-total_bars:]
