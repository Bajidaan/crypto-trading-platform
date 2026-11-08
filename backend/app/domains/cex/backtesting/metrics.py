"""Quantitative Performance Metrics for Algorithmic Backtests."""

from decimal import Decimal
import math
from typing import Any
from pydantic import BaseModel, ConfigDict

from app.core.decimal_utils import to_decimal


class BacktestMetrics(BaseModel):
    """Structured performance report for a backtesting execution run."""

    model_config = ConfigDict(frozen=True)

    initial_capital: Decimal
    final_capital: Decimal
    net_pnl: Decimal
    total_return_pct: Decimal
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: Decimal
    profit_factor: Decimal
    max_drawdown_usd: Decimal
    max_drawdown_pct: Decimal
    sharpe_ratio: Decimal
    sortino_ratio: Decimal
    avg_trade_pnl: Decimal
    avg_win: Decimal
    avg_loss: Decimal


def compute_backtest_metrics(
    initial_capital: Decimal,
    trades: list[dict[str, Any]],
    equity_curve: list[Decimal],
) -> BacktestMetrics:
    """Calculate comprehensive institutional backtest performance metrics."""
    if not trades or not equity_curve:
        return BacktestMetrics(
            initial_capital=initial_capital,
            final_capital=initial_capital,
            net_pnl=Decimal("0"),
            total_return_pct=Decimal("0"),
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate_pct=Decimal("0"),
            profit_factor=Decimal("0"),
            max_drawdown_usd=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            sharpe_ratio=Decimal("0"),
            sortino_ratio=Decimal("0"),
            avg_trade_pnl=Decimal("0"),
            avg_win=Decimal("0"),
            avg_loss=Decimal("0"),
        )

    final_capital = equity_curve[-1]
    net_pnl = final_capital - initial_capital
    total_return_pct = (net_pnl / initial_capital) * Decimal("100")

    total_trades = len(trades)
    winning_trades = sum(1 for t in trades if t["net_pnl"] > Decimal("0"))
    losing_trades = sum(1 for t in trades if t["net_pnl"] < Decimal("0"))
    win_rate_pct = (
        (Decimal(str(winning_trades)) / Decimal(str(total_trades))) * Decimal("100")
        if total_trades > 0
        else Decimal("0")
    )

    gross_profit = sum(
        (t["net_pnl"] for t in trades if t["net_pnl"] > Decimal("0")), Decimal("0")
    )
    gross_loss = sum(
        (abs(t["net_pnl"]) for t in trades if t["net_pnl"] < Decimal("0")), Decimal("0")
    )

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > Decimal("0")
        else (Decimal("999.0") if gross_profit > Decimal("0") else Decimal("0"))
    )

    avg_win = gross_profit / Decimal(str(winning_trades)) if winning_trades > 0 else Decimal("0")
    avg_loss = gross_loss / Decimal(str(losing_trades)) if losing_trades > 0 else Decimal("0")
    avg_trade_pnl = net_pnl / Decimal(str(total_trades)) if total_trades > 0 else Decimal("0")

    # Max Drawdown calculation from equity curve
    peak = equity_curve[0]
    max_dd_usd = Decimal("0")
    max_dd_pct = Decimal("0")

    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd_usd = peak - eq
        dd_pct = (dd_usd / peak) * Decimal("100") if peak > Decimal("0") else Decimal("0")
        if dd_usd > max_dd_usd:
            max_dd_usd = dd_usd
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    # Sharpe and Sortino ratio estimation based on per-trade returns
    returns = [float(t["net_pnl"] / initial_capital) for t in trades]
    if len(returns) >= 2:
        mean_ret = sum(returns) / len(returns)
        std_ret = math.sqrt(sum((r - mean_ret) ** 2 for r in returns) / len(returns))
        sharpe = Decimal(str(round((mean_ret / std_ret) * math.sqrt(252), 2))) if std_ret > 0 else Decimal("0")

        downside_returns = [r for r in returns if r < 0]
        if downside_returns:
            downside_std = math.sqrt(
                sum(r**2 for r in downside_returns) / len(downside_returns)
            )
            sortino = (
                Decimal(str(round((mean_ret / downside_std) * math.sqrt(252), 2)))
                if downside_std > 0
                else Decimal("0")
            )
        else:
            sortino = Decimal("999.0") if mean_ret > 0 else Decimal("0")
    else:
        sharpe = Decimal("0")
        sortino = Decimal("0")

    return BacktestMetrics(
        initial_capital=initial_capital,
        final_capital=final_capital,
        net_pnl=net_pnl,
        total_return_pct=total_return_pct,
        total_trades=total_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        win_rate_pct=win_rate_pct,
        profit_factor=profit_factor,
        max_drawdown_usd=max_dd_usd,
        max_drawdown_pct=max_dd_pct,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        avg_trade_pnl=avg_trade_pnl,
        avg_win=avg_win,
        avg_loss=avg_loss,
    )
