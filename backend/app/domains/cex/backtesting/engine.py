"""Deterministic Event-Driven Backtesting Engine with Exact Slippage and Fee Modeling."""

from decimal import Decimal
from typing import Any
from pydantic import BaseModel, ConfigDict

from app.core.decimal_utils import calculate_position_pnl, to_decimal
from app.core.events import MarketCandleEvent
from app.domains.cex.adapters.base import CandleBar
from app.domains.cex.backtesting.metrics import BacktestMetrics, compute_backtest_metrics
from app.domains.cex.market_data.buffer import CandleBuffer
from app.domains.cex.strategies.base import IStrategy
from app.models.enums import ExchangeId, MarketType, PositionSide


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


class BacktestResult(BaseModel):
    """Full backtest results including metrics, trade history, and equity curve."""

    model_config = ConfigDict(frozen=True)

    strategy_name: str
    timeframe: str
    metrics: BacktestMetrics
    trades: list[dict[str, Any]]
    equity_curve: list[Decimal]


class DeterministicBacktestEngine:
    """Simulates realistic historical execution with slippage, fees, and multi-tier SL/TP."""

    def __init__(
        self,
        strategy: IStrategy,
        initial_capital: Decimal = Decimal("10000.00"),
        maker_fee: Decimal = Decimal("0.0002"),
        taker_fee: Decimal = Decimal("0.0005"),
        slippage_pct: Decimal = Decimal("0.0005"),  # 0.05% realistic market order slippage
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

    def run(self, candles: list[CandleBar]) -> BacktestResult:
        """Execute historical simulation bar-by-bar."""
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
        buffer = CandleBuffer(symbol=symbol, timeframe=timeframe, max_size=len(candles) + 10)

        current_equity = self.initial_capital
        equity_curve: list[Decimal] = [current_equity]
        completed_trades: list[dict[str, Any]] = []

        # Active position state
        active_position: dict[str, Any] | None = None

        for bar in candles:
            buffer.update_or_append(bar)

            # 1. Manage open position exits on this bar
            if active_position is not None:
                side = active_position["side"]
                entry_price = active_position["entry_price"]
                qty = active_position["quantity"]
                stop_loss = active_position["stop_loss"]
                tp_targets = active_position.get("tp_targets", [])

                exit_triggered = False
                exit_price = Decimal("0")
                exit_reason = ""

                # Check Stop-Loss breach
                if side == PositionSide.LONG and bar.low <= stop_loss:
                    exit_price = stop_loss * (Decimal("1") - self.slippage_pct)
                    exit_reason = "STOP_LOSS"
                    exit_triggered = True
                elif side == PositionSide.SHORT and bar.high >= stop_loss:
                    exit_price = stop_loss * (Decimal("1") + self.slippage_pct)
                    exit_reason = "STOP_LOSS"
                    exit_triggered = True

                # Check Take-Profit breach if SL was not hit
                if not exit_triggered and tp_targets:
                    first_tp = tp_targets[0]
                    tp_price = first_tp.price
                    if side == PositionSide.LONG and bar.high >= tp_price:
                        exit_price = tp_price
                        exit_reason = f"TAKE_PROFIT_{first_tp.target_index}"
                        exit_triggered = True
                    elif side == PositionSide.SHORT and bar.low <= tp_price:
                        exit_price = tp_price
                        exit_reason = f"TAKE_PROFIT_{first_tp.target_index}"
                        exit_triggered = True

                if exit_triggered:
                    entry_notional = qty * entry_price
                    exit_notional = qty * exit_price
                    entry_fee = entry_notional * self.taker_fee
                    exit_fee = exit_notional * self.taker_fee
                    total_fee = entry_fee + exit_fee

                    gross_pnl, net_pnl = calculate_position_pnl(
                        side=side.value,
                        entry_price=entry_price,
                        current_or_exit_price=exit_price,
                        quantity=qty,
                        total_fees_usd=total_fee,
                    )
                    current_equity += net_pnl
                    equity_curve.append(current_equity)

                    completed_trades.append(
                        {
                            "symbol": symbol,
                            "side": side.value,
                            "entry_time": active_position["entry_time"],
                            "exit_time": bar.open_time.isoformat(),
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "quantity": qty,
                            "gross_pnl": gross_pnl,
                            "fee_paid": total_fee,
                            "net_pnl": net_pnl,
                            "exit_reason": exit_reason,
                        }
                    )
                    active_position = None
                    continue

            # 2. If no position active: evaluate strategy signals on closed candle
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

            if signals and active_position is None:
                sig = signals[0]
                risk_amount = current_equity * (self.risk_per_trade_pct / Decimal("100"))
                distance = abs(sig.signal_price - sig.stop_loss_price)
                if distance > Decimal("0"):
                    calc_qty = risk_amount / distance
                    # Cap quantity by max allowed leverage
                    max_qty_by_leverage = (
                        current_equity * Decimal(str(self.max_leverage))
                    ) / sig.signal_price
                    qty = min(calc_qty, max_qty_by_leverage)

                    # Apply slippage on entry
                    slip = self.slippage_pct if sig.side == PositionSide.LONG else -self.slippage_pct
                    entry_price = sig.signal_price * (Decimal("1") + slip)

                    active_position = {
                        "side": sig.side,
                        "entry_time": bar.open_time.isoformat(),
                        "entry_price": entry_price,
                        "quantity": qty,
                        "stop_loss": sig.stop_loss_price,
                        "tp_targets": sig.take_profit_targets,
                    }

        metrics = compute_backtest_metrics(self.initial_capital, completed_trades, equity_curve)
        return BacktestResult(
            strategy_name=self.strategy.name,
            timeframe=timeframe,
            metrics=metrics,
            trades=completed_trades,
            equity_curve=equity_curve,
        )
