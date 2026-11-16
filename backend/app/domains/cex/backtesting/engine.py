"""Deterministic Event-Driven Backtesting Engine with Slippage, Fees and a Buy-and-Hold Benchmark.

Execution model:
  * A strategy sees a bar only after it has closed. Its signal is filled at the OPEN of the
    next bar (no same-bar "delay-0" fills).
  * Entries and strategy exits are market orders: slippage and the taker fee apply.
  * Stop-losses fill at the stop price, or at the bar open when price gaps through the stop.
  * A position still open on the last bar is closed at that bar's close so it is counted.
"""

from decimal import Decimal
import math
from typing import Any
from pydantic import BaseModel, ConfigDict

from app.core.decimal_utils import calculate_position_pnl
from app.core.events import MarketCandleEvent, TradeSignalEvent
from app.domains.cex.adapters.base import CandleBar
from app.domains.cex.backtesting.metrics import BacktestMetrics, compute_backtest_metrics
from app.domains.cex.market_data.buffer import CandleBuffer, timeframe_to_seconds
from app.domains.cex.strategies.base import IStrategy
from app.models.enums import ExchangeId, PositionSide, SignalAction

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
SECONDS_PER_YEAR = 365 * 86400


class BacktestTradeRecord(BaseModel):
    """Execution record of a single completed backtest trade."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    side: PositionSide
    entry_time: str
    exit_time: str
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    gross_pnl: Decimal
    fee_paid: Decimal
    net_pnl: Decimal
    exit_reason: str


class BacktestComparison(BaseModel):
    """Mark-to-market strategy statistics next to a buy-and-hold benchmark on the same bars.

    Both are measured from the first bar the strategy could trade (after its warmup) and both
    pay the same fee and slippage. Sharpe ratios use per-bar returns, a zero risk-free rate,
    and are annualized by the number of bars in a year.
    """

    model_config = ConfigDict(frozen=True)

    bars: int
    start_time: str
    end_time: str
    benchmark_start_time: str
    strategy_return_pct: Decimal
    strategy_max_drawdown_pct: Decimal
    strategy_sharpe: Decimal
    buy_and_hold_return_pct: Decimal
    buy_and_hold_max_drawdown_pct: Decimal
    buy_and_hold_sharpe: Decimal
    time_in_market_pct: Decimal
    total_fees_paid: Decimal


class BacktestResult(BaseModel):
    """Full backtest results including metrics, trade history, and equity curves."""

    model_config = ConfigDict(frozen=True)

    strategy_name: str
    timeframe: str
    metrics: BacktestMetrics
    trades: list[dict[str, Any]]
    # Equity after each closed trade (starts with initial capital).
    equity_curve: list[Decimal]
    # Equity marked to market at every bar close.
    bar_equity_curve: list[Decimal] = []
    comparison: BacktestComparison | None = None


def _max_drawdown_pct(curve: list[Decimal]) -> Decimal:
    peak = curve[0] if curve else ZERO
    worst = ZERO
    for eq in curve:
        if eq > peak:
            peak = eq
        if peak > ZERO:
            dd = (peak - eq) / peak * HUNDRED
            if dd > worst:
                worst = dd
    return worst


def _annualized_sharpe(curve: list[Decimal], bars_per_year: float) -> Decimal:
    if len(curve) < 3:
        return ZERO
    values = [float(x) for x in curve]
    returns = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] > 0]
    if len(returns) < 2:
        return ZERO
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if var <= 0:
        return ZERO
    return Decimal(str(round(mean / math.sqrt(var) * math.sqrt(bars_per_year), 2)))


class DeterministicBacktestEngine:
    """Simulates historical execution bar-by-bar with slippage, fees, stops and strategy exits."""

    def __init__(
        self,
        strategy: IStrategy,
        initial_capital: Decimal = Decimal("10000.00"),
        maker_fee: Decimal = Decimal("0.0002"),
        taker_fee: Decimal = Decimal("0.0005"),
        slippage_pct: Decimal = Decimal("0.0005"),  # 0.05% market order slippage (a fraction)
        risk_per_trade_pct: Decimal = Decimal("1.0"),
        max_leverage: int = 3,
    ) -> None:
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.slippage_pct = slippage_pct
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_leverage = max_leverage

    # ------------------------------------------------------------------
    # Fill helpers
    # ------------------------------------------------------------------

    def _market_fill(self, raw_price: Decimal, side: PositionSide, is_entry: bool) -> Decimal:
        """Apply slippage against the trader: buys fill higher, sells fill lower."""
        is_buy = (side == PositionSide.LONG) == is_entry
        return raw_price * (ONE + self.slippage_pct if is_buy else ONE - self.slippage_pct)

    def _open_position(
        self,
        sig: TradeSignalEvent,
        bar: CandleBar,
        equity: Decimal,
    ) -> dict[str, Any] | None:
        fill = self._market_fill(bar.open, sig.side, is_entry=True)
        stop = sig.stop_loss_price

        # The market moved through the stop before we could enter: skip the trade.
        if sig.side == PositionSide.LONG and stop >= fill:
            return None
        if sig.side == PositionSide.SHORT and stop <= fill:
            return None
        if fill <= ZERO or equity <= ZERO:
            return None

        max_qty = equity * Decimal(str(self.max_leverage)) / fill
        if sig.target_weight is not None:
            qty = equity * sig.target_weight / fill
        else:
            risk_amount = equity * (self.risk_per_trade_pct / HUNDRED)
            qty = risk_amount / abs(fill - stop)
        qty = min(qty, max_qty)
        if qty <= ZERO:
            return None

        return {
            "side": sig.side,
            "entry_time": bar.open_time.isoformat(),
            "entry_price": fill,
            "quantity": qty,
            "stop_loss": stop,
            "tp_targets": sig.take_profit_targets,
        }

    def _close_position(
        self,
        position: dict[str, Any],
        exit_price: Decimal,
        exit_reason: str,
        exit_time: str,
        symbol: str,
    ) -> dict[str, Any]:
        qty = position["quantity"]
        entry_price = position["entry_price"]
        total_fee = (qty * entry_price + qty * exit_price) * self.taker_fee
        gross_pnl, net_pnl = calculate_position_pnl(
            side=position["side"].value,
            entry_price=entry_price,
            current_or_exit_price=exit_price,
            quantity=qty,
            total_fees_usd=total_fee,
        )
        return {
            "symbol": symbol,
            "side": position["side"].value,
            "entry_time": position["entry_time"],
            "exit_time": exit_time,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "quantity": qty,
            "gross_pnl": gross_pnl,
            "fee_paid": total_fee,
            "net_pnl": net_pnl,
            "exit_reason": exit_reason,
        }

    def _check_stop_and_target(
        self, position: dict[str, Any], bar: CandleBar
    ) -> tuple[Decimal, str] | None:
        """Return (exit_price, reason) if this bar hits the stop-loss or first take-profit."""
        side = position["side"]
        stop = position["stop_loss"]

        if side == PositionSide.LONG and bar.low <= stop:
            raw = min(stop, bar.open)  # gap down through the stop fills at the open
            return self._market_fill(raw, side, is_entry=False), "STOP_LOSS"
        if side == PositionSide.SHORT and bar.high >= stop:
            raw = max(stop, bar.open)
            return self._market_fill(raw, side, is_entry=False), "STOP_LOSS"

        tp_targets = position.get("tp_targets") or []
        if tp_targets:
            first_tp = tp_targets[0]
            if side == PositionSide.LONG and bar.high >= first_tp.price:
                return first_tp.price, f"TAKE_PROFIT_{first_tp.target_index}"
            if side == PositionSide.SHORT and bar.low <= first_tp.price:
                return first_tp.price, f"TAKE_PROFIT_{first_tp.target_index}"
        return None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(
        self,
        candles: list[CandleBar],
        context_candles: dict[str, list[CandleBar]] | None = None,
    ) -> BacktestResult:
        """Execute the historical simulation bar-by-bar.

        `context_candles` supplies history for the strategy's `context_symbols` (for example a
        benchmark). Each context bar becomes visible once its open time is not later than the
        bar being evaluated.
        """
        if not candles:
            empty_metrics = compute_backtest_metrics(self.initial_capital, [], [])
            return BacktestResult(
                strategy_name=self.strategy.name,
                timeframe=self.strategy.timeframe,
                metrics=empty_metrics,
                trades=[],
                equity_curve=[self.initial_capital],
            )

        symbol = candles[0].symbol
        market_type = candles[0].market_type
        timeframe = candles[0].timeframe
        warmup = self.strategy.required_warmup_bars
        buffer_size = max(500, warmup * 3)
        buffer = CandleBuffer(
            symbol=symbol, timeframe=timeframe, market_type=market_type, max_size=buffer_size
        )

        # Context (benchmark) buffers, fed in step with the main series.
        context_series: dict[str, list[CandleBar]] = {}
        context_cursor: dict[str, int] = {}
        context_buffers: dict[str, CandleBuffer] = {}
        for ctx_symbol in self.strategy.context_symbols:
            if ctx_symbol == symbol:
                context_buffers[ctx_symbol] = buffer
                continue
            context_buffers[ctx_symbol] = CandleBuffer(
                symbol=ctx_symbol, timeframe=timeframe, market_type=market_type, max_size=buffer_size
            )
            context_series[ctx_symbol] = sorted(
                (context_candles or {}).get(ctx_symbol, []), key=lambda c: c.open_time
            )
            context_cursor[ctx_symbol] = 0
        self.strategy.context_buffers = context_buffers

        current_equity = self.initial_capital
        equity_curve: list[Decimal] = [current_equity]
        bar_equity_curve: list[Decimal] = []
        completed_trades: list[dict[str, Any]] = []
        active_position: dict[str, Any] | None = None
        pending_signal: TradeSignalEvent | None = None
        bars_in_market = 0

        def close(position: dict[str, Any], price: Decimal, reason: str, when: str) -> None:
            nonlocal current_equity
            trade = self._close_position(position, price, reason, when, symbol)
            current_equity += trade["net_pnl"]
            equity_curve.append(current_equity)
            completed_trades.append(trade)

        for bar in candles:
            # 1. Fill the signal produced on the previous bar at this bar's open.
            if pending_signal is not None:
                sig = pending_signal
                pending_signal = None
                if sig.action == SignalAction.EXIT:
                    if active_position is not None and active_position["side"] == sig.side:
                        exit_price = self._market_fill(bar.open, sig.side, is_entry=False)
                        close(active_position, exit_price, "STRATEGY_EXIT", bar.open_time.isoformat())
                        active_position = None
                elif active_position is None:
                    active_position = self._open_position(sig, bar, current_equity)

            # 2. Stop-loss / take-profit on this bar's range.
            if active_position is not None:
                hit = self._check_stop_and_target(active_position, bar)
                if hit is not None:
                    close(active_position, hit[0], hit[1], bar.open_time.isoformat())
                    active_position = None

            # 3. The bar closes: reveal it (and matching context bars) to the strategy.
            buffer.update_or_append(bar)
            for ctx_symbol, series in context_series.items():
                idx = context_cursor[ctx_symbol]
                while idx < len(series) and series[idx].open_time <= bar.open_time:
                    context_buffers[ctx_symbol].update_or_append(series[idx])
                    idx += 1
                context_cursor[ctx_symbol] = idx

            # 4. Mark equity to market at the close.
            if active_position is not None:
                bars_in_market += 1
                qty = active_position["quantity"]
                entry = active_position["entry_price"]
                direction = ONE if active_position["side"] == PositionSide.LONG else -ONE
                unrealized = direction * qty * (bar.close - entry)
                bar_equity_curve.append(current_equity + unrealized - qty * entry * self.taker_fee)
            else:
                bar_equity_curve.append(current_equity)

            # 5. Evaluate the strategy on the closed bar; its signal fills on the next bar.
            self.strategy.position_state = (
                {symbol: active_position["side"]} if active_position is not None else {}
            )
            candle_event = MarketCandleEvent(
                exchange_id=ExchangeId.BINANCE,
                symbol=symbol,
                market_type=market_type,
                timeframe=timeframe,
                open_time=bar.open_time,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                is_closed=bar.is_closed,
            )
            signals = self.strategy.on_candle(candle_event, buffer)
            wanted = SignalAction.EXIT if active_position is not None else SignalAction.ENTRY
            pending_signal = next((s for s in signals if s.action == wanted), None)

        # A position still open at the end is closed at the last close so it is counted.
        last_bar = candles[-1]
        if active_position is not None:
            exit_price = self._market_fill(last_bar.close, active_position["side"], is_entry=False)
            close(active_position, exit_price, "END_OF_DATA", last_bar.open_time.isoformat())
            active_position = None
            bar_equity_curve[-1] = current_equity

        metrics = compute_backtest_metrics(self.initial_capital, completed_trades, equity_curve)
        comparison = self._build_comparison(
            candles=candles,
            bar_equity_curve=bar_equity_curve,
            completed_trades=completed_trades,
            bars_in_market=bars_in_market,
            warmup=warmup,
        )
        return BacktestResult(
            strategy_name=self.strategy.name,
            timeframe=timeframe,
            metrics=metrics,
            trades=completed_trades,
            equity_curve=equity_curve,
            bar_equity_curve=bar_equity_curve,
            comparison=comparison,
        )

    def _build_comparison(
        self,
        candles: list[CandleBar],
        bar_equity_curve: list[Decimal],
        completed_trades: list[dict[str, Any]],
        bars_in_market: int,
        warmup: int,
    ) -> BacktestComparison:
        # The strategy's first possible fill is the open of bar index `warmup`.
        start_idx = min(max(warmup, 0), len(candles) - 1)
        start_bar = candles[start_idx]
        last_bar = candles[-1]

        # Buy and hold: all capital in at the same first fill, same fee and slippage, out at the end.
        entry = self._market_fill(start_bar.open, PositionSide.LONG, is_entry=True)
        qty = self.initial_capital / (entry * (ONE + self.taker_fee))
        hold_curve = [qty * c.close for c in candles[start_idx:]]
        exit_price = self._market_fill(last_bar.close, PositionSide.LONG, is_entry=False)
        hold_curve[-1] = qty * exit_price * (ONE - self.taker_fee)
        hold_curve = [self.initial_capital] + hold_curve

        strat_curve = [self.initial_capital] + bar_equity_curve[start_idx:]
        tradable_bars = len(candles) - start_idx
        bars_per_year = SECONDS_PER_YEAR / timeframe_to_seconds(candles[0].timeframe)

        def pct(final: Decimal) -> Decimal:
            return (final - self.initial_capital) / self.initial_capital * HUNDRED

        return BacktestComparison(
            bars=len(candles),
            start_time=candles[0].open_time.isoformat(),
            end_time=last_bar.open_time.isoformat(),
            benchmark_start_time=start_bar.open_time.isoformat(),
            strategy_return_pct=pct(strat_curve[-1]),
            strategy_max_drawdown_pct=_max_drawdown_pct(strat_curve),
            strategy_sharpe=_annualized_sharpe(strat_curve, bars_per_year),
            buy_and_hold_return_pct=pct(hold_curve[-1]),
            buy_and_hold_max_drawdown_pct=_max_drawdown_pct(hold_curve),
            buy_and_hold_sharpe=_annualized_sharpe(hold_curve, bars_per_year),
            time_in_market_pct=Decimal(str(bars_in_market)) / Decimal(str(tradable_bars)) * HUNDRED,
            total_fees_paid=sum((t["fee_paid"] for t in completed_trades), ZERO),
        )
