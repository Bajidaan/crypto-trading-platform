import React, { useEffect, useState } from 'react';
import {
  ArrowDownRight,
  ArrowUpRight,
  Award,
  BarChart3,
  CheckCircle2,
  Filter,
  RefreshCw,
  TrendingDown,
  TrendingUp,
  X,
} from 'lucide-react';
import { api } from '../services/api';

interface AnalyticsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export const AnalyticsModal: React.FC<AnalyticsModalProps> = ({ isOpen, onClose }) => {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [strategyFilter, setStrategyFilter] = useState<string>('ALL');
  const [outcomeFilter, setOutcomeFilter] = useState<string>('ALL');

  const fetchAnalytics = async () => {
    try {
      setLoading(true);
      const res = await api.getPositionsAnalytics();
      setData(res);
    } catch (err) {
      console.error('Failed to load analytics:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      fetchAnalytics();
    }
  }, [isOpen]);

  if (!isOpen) return null;

  const summary = data?.summary || {
    total_trades: 0,
    winning_trades: 0,
    losing_trades: 0,
    win_rate_pct: 0,
    total_realized_pnl_usd: 0,
    profit_factor: 0,
    total_fees_usd: 0,
  };

  const strategies: any[] = data?.strategy_attribution || [];
  const trades: any[] = data?.trades || [];

  // Filter trades
  const filteredTrades = trades.filter((t: any) => {
    if (strategyFilter !== 'ALL' && t.strategy_name !== strategyFilter) {
      return false;
    }
    if (outcomeFilter === 'WIN' && t.realized_pnl_usd <= 0) return false;
    if (outcomeFilter === 'LOSS' && t.realized_pnl_usd >= 0) return false;
    if (outcomeFilter === 'TP' && !String(t.exit_reason).toUpperCase().includes('TAKE_PROFIT')) return false;
    if (outcomeFilter === 'SL' && !String(t.exit_reason).toUpperCase().includes('STOP_LOSS')) return false;
    return true;
  });

  const isNetProfit = summary.total_realized_pnl_usd >= 0;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 backdrop-blur-sm p-4 overflow-y-auto">
      <div className="bg-darkpanel border border-darkborder rounded-2xl max-w-6xl w-full p-6 shadow-2xl flex flex-col max-h-[90vh]">
        {/* Modal Header */}
        <div className="flex items-center justify-between pb-4 border-b border-darkborder">
          <div className="flex items-center space-x-3">
            <div className="p-2 rounded-xl bg-blue-500/10 border border-blue-500/30 text-blue-400">
              <BarChart3 className="w-6 h-6" />
            </div>
            <div>
              <h2 className="text-base font-bold text-white tracking-wide">
                Completed Trades & Strategy Attribution Journal
              </h2>
              <p className="text-xs text-gray-400">
                Audited outcomes, individual strategy edge, and fee accounting.
              </p>
            </div>
          </div>

          <div className="flex items-center space-x-2">
            <button
              onClick={fetchAnalytics}
              disabled={loading}
              className="p-2 rounded-lg bg-darkcard border border-darkborder text-gray-300 hover:text-white hover:bg-darkborder/50 transition"
              title="Refresh Analytics"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin text-blue-400' : ''}`} />
            </button>
            <button
              onClick={onClose}
              className="p-2 rounded-lg bg-darkcard border border-darkborder text-gray-400 hover:text-white hover:bg-darkborder/50 transition"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Content Body */}
        <div className="flex-1 overflow-y-auto pt-4 space-y-6 pr-1">
          {/* Top Macro Summary KPI Cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {/* Realized PnL */}
            <div className="bg-darkcard border border-darkborder rounded-xl p-3.5 flex flex-col justify-between">
              <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
                Total Realized PnL
              </span>
              <div className="mt-2 flex items-baseline space-x-2">
                <span
                  className={`text-xl font-bold font-mono ${
                    isNetProfit ? 'text-emerald-400' : 'text-rose-400'
                  }`}
                >
                  {isNetProfit ? '+' : ''}${Number(summary.total_realized_pnl_usd).toFixed(2)}
                </span>
                {isNetProfit ? (
                  <TrendingUp className="w-4 h-4 text-emerald-400" />
                ) : (
                  <TrendingDown className="w-4 h-4 text-rose-400" />
                )}
              </div>
              <span className="text-[10px] text-gray-400 mt-1">Across all completed positions</span>
            </div>

            {/* Win Rate */}
            <div className="bg-darkcard border border-darkborder rounded-xl p-3.5 flex flex-col justify-between">
              <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
                Win Rate
              </span>
              <div className="mt-2 flex items-baseline space-x-2">
                <span className="text-xl font-bold font-mono text-white">
                  {summary.win_rate_pct.toFixed(1)}%
                </span>
                <span className="text-[11px] text-gray-400">
                  ({summary.winning_trades}W / {summary.losing_trades}L)
                </span>
              </div>
              <div className="w-full bg-darkbg rounded-full h-1.5 mt-2 overflow-hidden flex">
                <div
                  className="bg-emerald-500 h-full"
                  style={{ width: `${Math.min(100, Math.max(0, summary.win_rate_pct))}%` }}
                />
                <div
                  className="bg-rose-500 h-full"
                  style={{ width: `${Math.min(100, 100 - summary.win_rate_pct)}%` }}
                />
              </div>
            </div>

            {/* Profit Factor */}
            <div className="bg-darkcard border border-darkborder rounded-xl p-3.5 flex flex-col justify-between">
              <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
                Profit Factor
              </span>
              <div className="mt-2 flex items-baseline space-x-2">
                <span className="text-xl font-bold font-mono text-blue-400">
                  {summary.profit_factor >= 999 ? '∞ (Zero Loss)' : summary.profit_factor.toFixed(2)}
                </span>
              </div>
              <span className="text-[10px] text-gray-400 mt-1">Gross Gains / Gross Losses</span>
            </div>

            {/* Total Fees */}
            <div className="bg-darkcard border border-darkborder rounded-xl p-3.5 flex flex-col justify-between">
              <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
                Total Fees Paid
              </span>
              <div className="mt-2 flex items-baseline space-x-2">
                <span className="text-xl font-bold font-mono text-gray-300">
                  ${Number(summary.total_fees_usd).toFixed(2)}
                </span>
              </div>
              <span className="text-[10px] text-gray-400 mt-1">Simulated taker/maker fees</span>
            </div>
          </div>

          {/* Individual Strategy Attribution Section */}
          <div>
            <h3 className="text-xs font-bold text-gray-300 uppercase tracking-wider mb-2.5 flex items-center space-x-1.5">
              <Award className="w-3.5 h-3.5 text-blue-400" />
              <span>Strategy Attribution Breakdown</span>
            </h3>

            {strategies.length === 0 ? (
              <div className="bg-darkcard/50 border border-darkborder/50 rounded-xl p-4 text-center text-xs text-gray-500">
                No strategy attribution recorded yet. Strategies will appear here as they complete trades.
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                {strategies.map((strat) => {
                  const sPnl = strat.realized_pnl_usd ?? 0;
                  const isPositive = sPnl >= 0;

                  return (
                    <div
                      key={strat.strategy_name}
                      className="bg-darkcard border border-darkborder rounded-xl p-3.5 flex flex-col justify-between hover:border-blue-500/40 transition"
                    >
                      <div className="flex items-center justify-between pb-2 border-b border-darkborder/50">
                        <span className="font-bold text-white text-xs truncate max-w-[170px]">
                          {strat.strategy_name}
                        </span>
                        <span
                          className={`font-mono text-xs font-bold ${
                            isPositive ? 'text-emerald-400' : 'text-rose-400'
                          }`}
                        >
                          {isPositive ? '+' : ''}${sPnl.toFixed(2)}
                        </span>
                      </div>

                      <div className="grid grid-cols-3 gap-2 py-2.5 text-center text-[11px] font-mono">
                        <div>
                          <span className="text-gray-400 block text-[9px] uppercase">Trades</span>
                          <span className="font-bold text-white">{strat.total_trades}</span>
                        </div>
                        <div>
                          <span className="text-gray-400 block text-[9px] uppercase">Win Rate</span>
                          <span className="font-bold text-gray-200">{strat.win_rate_pct.toFixed(0)}%</span>
                        </div>
                        <div>
                          <span className="text-gray-400 block text-[9px] uppercase">Profit Factor</span>
                          <span className="font-bold text-blue-400">
                            {strat.profit_factor >= 999 ? '∞' : strat.profit_factor.toFixed(1)}
                          </span>
                        </div>
                      </div>

                      {/* Exit reason tags */}
                      <div className="flex flex-wrap gap-1 pt-1 border-t border-darkborder/40">
                        {Object.entries(strat.exit_reasons || {}).map(([reason, count]) => (
                          <span
                            key={reason}
                            className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-darkbg border border-darkborder text-gray-400"
                          >
                            {reason}: <strong className="text-gray-200">{Number(count)}</strong>
                          </span>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Completed Trades Journal Table */}
          <div className="space-y-3">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
              <div className="flex items-center space-x-2">
                <Filter className="w-4 h-4 text-blue-400" />
                <h3 className="text-xs font-bold text-gray-300 uppercase tracking-wider">
                  Completed Trades Journal ({filteredTrades.length})
                </h3>
              </div>

              {/* Filters */}
              <div className="flex items-center space-x-2 text-xs">
                {/* Filter by strategy */}
                <select
                  value={strategyFilter}
                  onChange={(e) => setStrategyFilter(e.target.value)}
                  className="bg-darkcard border border-darkborder rounded-lg px-2.5 py-1 text-gray-200 text-xs outline-none focus:border-blue-500 font-mono"
                >
                  <option value="ALL">All Strategies</option>
                  {strategies.map((s) => (
                    <option key={s.strategy_name} value={s.strategy_name}>
                      {s.strategy_name}
                    </option>
                  ))}
                </select>

                {/* Filter by outcome */}
                <select
                  value={outcomeFilter}
                  onChange={(e) => setOutcomeFilter(e.target.value)}
                  className="bg-darkcard border border-darkborder rounded-lg px-2.5 py-1 text-gray-200 text-xs outline-none focus:border-blue-500 font-mono"
                >
                  <option value="ALL">All Outcomes</option>
                  <option value="WIN">Profitable Trades Only</option>
                  <option value="LOSS">Losses Only</option>
                  <option value="TP">Take-Profit Hits</option>
                  <option value="SL">Stop-Loss Hits</option>
                </select>
              </div>
            </div>

            {filteredTrades.length === 0 ? (
              <div className="bg-darkcard border border-darkborder rounded-xl p-8 text-center text-xs text-gray-400">
                <CheckCircle2 className="w-8 h-8 text-gray-600 mx-auto mb-2" />
                <p className="font-semibold text-gray-300">No completed trades match your filter.</p>
                <p className="text-[11px] text-gray-500 mt-1">
                  Active positions will automatically move here when they hit Take-Profit, Stop-Loss, or are closed.
                </p>
              </div>
            ) : (
              <div className="overflow-x-auto border border-darkborder rounded-xl bg-darkcard">
                <table className="w-full text-left text-xs font-mono">
                  <thead>
                    <tr className="text-gray-400 border-b border-darkborder bg-darkcard/80">
                      <th className="py-2.5 px-3 font-semibold">Symbol & Strategy</th>
                      <th className="py-2.5 px-3 font-semibold">Side / Lev</th>
                      <th className="py-2.5 px-3 font-semibold">Entry → Exit</th>
                      <th className="py-2.5 px-3 font-semibold">Realized PnL</th>
                      <th className="py-2.5 px-3 font-semibold">Outcome / Reason</th>
                      <th className="py-2.5 px-3 font-semibold">Duration</th>
                      <th className="py-2.5 px-3 font-semibold">Closed Time</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-darkborder/50">
                    {filteredTrades.map((t: any) => {
                      const isProfit = t.realized_pnl_usd >= 0;
                      const isLong = t.side === 'LONG';

                      return (
                        <tr key={t.id} className="hover:bg-darkpanel/40 transition">
                          {/* Symbol & Strategy */}
                          <td className="py-3 px-3">
                            <div className="font-bold text-white">{t.symbol}</div>
                            <span className="text-[10px] text-blue-400/90 font-sans font-medium">
                              {t.strategy_name}
                            </span>
                          </td>

                          {/* Side & Leverage */}
                          <td className="py-3 px-3">
                            <span
                              className={`inline-flex items-center font-bold px-1.5 py-0.5 rounded text-[10px] ${
                                isLong
                                  ? 'bg-emerald-500/20 text-emerald-400'
                                  : 'bg-rose-500/20 text-rose-400'
                              }`}
                            >
                              {isLong ? (
                                <ArrowUpRight className="w-3 h-3 mr-0.5" />
                              ) : (
                                <ArrowDownRight className="w-3 h-3 mr-0.5" />
                              )}
                              {t.side} {t.leverage}x
                            </span>
                          </td>

                          {/* Entry -> Exit */}
                          <td className="py-3 px-3 text-gray-300">
                            <div>${Number(t.entry_price).toFixed(2)}</div>
                            <div className="text-[10px] text-gray-400">
                              → ${Number(t.exit_price).toFixed(2)}
                            </div>
                          </td>

                          {/* Realized PnL */}
                          <td className="py-3 px-3">
                            <div
                              className={`font-bold ${
                                isProfit ? 'text-emerald-400' : 'text-rose-400'
                              }`}
                            >
                              {isProfit ? '+' : ''}${Number(t.realized_pnl_usd).toFixed(2)}
                            </div>
                            <div
                              className={`text-[10px] ${
                                isProfit ? 'text-emerald-500/80' : 'text-rose-500/80'
                              }`}
                            >
                              ({isProfit ? '+' : ''}{Number(t.realized_pnl_pct).toFixed(2)}%)
                            </div>
                          </td>

                          {/* Outcome / Reason */}
                          <td className="py-3 px-3">
                            <span
                              className={`px-2 py-0.5 rounded text-[10px] font-semibold border ${
                                t.exit_reason.includes('TAKE_PROFIT')
                                  ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
                                  : t.exit_reason.includes('STOP_LOSS')
                                  ? 'bg-rose-500/20 text-rose-300 border-rose-500/30'
                                  : 'bg-amber-500/20 text-amber-300 border-amber-500/30'
                              }`}
                            >
                              {t.exit_reason}
                            </span>
                          </td>

                          {/* Duration */}
                          <td className="py-3 px-3 text-gray-400 text-[11px] font-sans">
                            {t.duration || '< 1m'}
                          </td>

                          {/* Closed Time */}
                          <td className="py-3 px-3 text-gray-400 text-[11px]">
                            {t.closed_at ? new Date(t.closed_at).toLocaleTimeString() : 'Recently'}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default AnalyticsModal;
