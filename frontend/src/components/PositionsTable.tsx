import React, { useState } from 'react';
import { ArrowDownRight, ArrowUpRight, History, ShieldCheck, XCircle } from 'lucide-react';
import type { PositionItem } from '../types';

interface PositionsTableProps {
  positions: PositionItem[];
  onClosePosition: (id: string) => void;
}

export const PositionsTable: React.FC<PositionsTableProps> = ({
  positions,
  onClosePosition,
}) => {
  const [activeTab, setActiveTab] = useState<'OPEN' | 'CLOSED'>('OPEN');

  const openPositions = positions.filter((p) => p.status === 'OPEN');
  const closedPositions = positions.filter((p) => p.status === 'CLOSED');

  return (
    <div className="bg-darkpanel border border-darkborder rounded-xl p-4 shadow-lg flex flex-col">
      {/* Header with Tabs */}
      <div className="flex items-center justify-between pb-3 mb-3 border-b border-darkborder">
        <div className="flex items-center space-x-4">
          <button
            onClick={() => setActiveTab('OPEN')}
            className={`flex items-center space-x-2 pb-1 text-sm font-bold uppercase tracking-wider transition border-b-2 ${
              activeTab === 'OPEN'
                ? 'text-white border-emerald-400'
                : 'text-gray-400 border-transparent hover:text-gray-200'
            }`}
          >
            <ShieldCheck className="w-4 h-4 text-emerald-400" />
            <span>Active Positions ({openPositions.length})</span>
          </button>

          <button
            onClick={() => setActiveTab('CLOSED')}
            className={`flex items-center space-x-2 pb-1 text-sm font-bold uppercase tracking-wider transition border-b-2 ${
              activeTab === 'CLOSED'
                ? 'text-white border-blue-400'
                : 'text-gray-400 border-transparent hover:text-gray-200'
            }`}
          >
            <History className="w-4 h-4 text-blue-400" />
            <span>Closed History ({closedPositions.length})</span>
          </button>
        </div>

        <span className="text-xs text-gray-400">
          Hybrid SL Airbag + Dynamic Trailing TP
        </span>
      </div>

      {/* ACTIVE POSITIONS TAB */}
      {activeTab === 'OPEN' && (
        openPositions.length === 0 ? (
          <div className="py-8 text-center text-gray-500 text-xs">
            No open positions. Algorithmic strategies are actively scanning for entry triggers.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs font-mono">
              <thead>
                <tr className="text-gray-400 border-b border-darkborder pb-2">
                  <th className="py-2 px-3 font-medium">Symbol / Mode</th>
                  <th className="py-2 px-3 font-medium">Side / Lev</th>
                  <th className="py-2 px-3 font-medium">Size</th>
                  <th className="py-2 px-3 font-medium">Entry Price</th>
                  <th className="py-2 px-3 font-medium">Mark Price</th>
                  <th className="py-2 px-3 font-medium">Unrealized PnL</th>
                  <th className="py-2 px-3 font-medium">Hard SL (Airbag)</th>
                  <th className="py-2 px-3 font-medium">Target TP</th>
                  <th className="py-2 px-3 font-medium text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-darkborder/50">
                {openPositions.map((p) => {
                  const isLong = p.side === 'LONG';
                  const isProfitable = p.unrealized_pnl_usd >= 0;

                  return (
                    <tr key={p.id} className="hover:bg-darkcard/50 transition">
                      {/* Symbol & Mode & Strategy */}
                      <td className="py-3 px-3">
                        <div className="flex items-center space-x-1.5">
                          <span className="font-bold text-white">{p.symbol}</span>
                          <span
                            className={`text-[10px] font-mono px-1.5 py-0.5 rounded font-bold uppercase tracking-wider ${
                              (p.strategy_abbr || '').includes('DCA')
                                ? 'bg-purple-500/20 text-purple-300 border border-purple-500/30'
                                : (p.strategy_abbr || '').includes('EMA')
                                ? 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30'
                                : 'bg-amber-500/20 text-amber-300 border border-amber-500/30'
                            }`}
                            title={p.strategy_name || 'Strategy'}
                          >
                            {p.strategy_abbr || 'STRAT'}
                          </span>
                        </div>
                        <div className="flex items-center space-x-1 mt-0.5">
                          <span
                            className={`text-[10px] px-1.5 py-0.2 rounded font-semibold ${
                              p.execution_mode === 'LIVE'
                                ? 'bg-rose-500/20 text-rose-300'
                                : 'bg-blue-500/20 text-blue-300'
                            }`}
                          >
                            {p.execution_mode}
                          </span>
                          <span className="text-[10px] text-gray-400 truncate max-w-[130px]" title={p.strategy_name || ''}>
                            {p.strategy_name || p.market_type.replace('_', ' ')}
                          </span>
                        </div>
                      </td>

                      {/* Side & Leverage */}
                      <td className="py-3 px-3">
                        <div className="flex items-center space-x-1">
                          <span
                            className={`flex items-center font-bold px-1.5 py-0.5 rounded text-[11px] ${
                              isLong
                                ? 'bg-emerald-500/20 text-emerald-400'
                                : 'bg-rose-500/20 text-rose-400'
                            }`}
                          >
                            {isLong ? (
                              <ArrowUpRight className="w-3.5 h-3.5 mr-0.5" />
                            ) : (
                              <ArrowDownRight className="w-3.5 h-3.5 mr-0.5" />
                            )}
                            {p.side}
                          </span>
                          <span className="text-gray-300 font-bold">{p.leverage}x</span>
                        </div>
                        <span className="text-[10px] text-gray-400 uppercase mt-0.5 block">
                          {p.margin_mode || 'ISOLATED'}
                        </span>
                      </td>

                      {/* Size */}
                      <td className="py-3 px-3 font-semibold text-gray-200">
                        {Number(p.current_qty).toLocaleString(undefined, {
                          maximumFractionDigits: 4,
                        })}
                      </td>

                      {/* Entry Price */}
                      <td className="py-3 px-3 text-gray-300">
                        ${Number(p.entry_price).toLocaleString(undefined, {
                          minimumFractionDigits: 2,
                          maximumFractionDigits: 3,
                        })}
                      </td>

                      {/* Current Mark Price */}
                      <td className="py-3 px-3 font-bold text-white">
                        ${Number(p.current_price ?? p.entry_price ?? 0).toLocaleString(undefined, {
                          minimumFractionDigits: 2,
                          maximumFractionDigits: 3,
                        })}
                      </td>

                      {/* Unrealized PnL */}
                      <td className="py-3 px-3">
                        <div
                          className={`font-bold ${
                            isProfitable ? 'text-emerald-400' : 'text-rose-400'
                          }`}
                        >
                          {isProfitable ? '+' : ''}
                          ${Number(p.unrealized_pnl_usd).toFixed(2)}
                        </div>
                        <div
                          className={`text-[10px] ${
                            isProfitable ? 'text-emerald-500/80' : 'text-rose-500/80'
                          }`}
                        >
                          ({isProfitable ? '+' : ''}
                          {Number(p.unrealized_pnl_pct).toFixed(2)}%)
                        </div>
                      </td>

                      {/* Hard Stop Loss */}
                      <td className="py-3 px-3 font-mono text-rose-400">
                        {p.stop_loss_price
                          ? `$${Number(p.stop_loss_price).toLocaleString(undefined, {
                              minimumFractionDigits: 2,
                              maximumFractionDigits: 3,
                            })}`
                          : 'None'}
                      </td>

                      {/* Target Take Profit */}
                      <td className="py-3 px-3 font-mono text-emerald-400">
                        {p.take_profit_price
                          ? `$${Number(p.take_profit_price).toLocaleString(undefined, {
                              minimumFractionDigits: 2,
                              maximumFractionDigits: 3,
                            })}`
                          : 'Trailing'}
                      </td>

                      {/* Action */}
                      <td className="py-3 px-3 text-right">
                        <button
                          onClick={() => onClosePosition(p.id)}
                          className="px-2 py-1 bg-rose-600/20 hover:bg-rose-600/40 text-rose-400 border border-rose-500/30 rounded text-xs transition"
                          title="Emergency Market Close Position"
                        >
                          <XCircle className="w-3.5 h-3.5" />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )
      )}

      {/* CLOSED POSITIONS HISTORY TAB */}
      {activeTab === 'CLOSED' && (
        closedPositions.length === 0 ? (
          <div className="py-8 text-center text-gray-500 text-xs">
            No closed positions yet. Positions will appear here when they hit Take-Profit or Stop-Loss.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs font-mono">
              <thead>
                <tr className="text-gray-400 border-b border-darkborder pb-2">
                  <th className="py-2 px-3 font-medium">Symbol / Mode</th>
                  <th className="py-2 px-3 font-medium">Side / Lev</th>
                  <th className="py-2 px-3 font-medium">Entry Price</th>
                  <th className="py-2 px-3 font-medium">Realized PnL</th>
                  <th className="py-2 px-3 font-medium">Exit Reason</th>
                  <th className="py-2 px-3 font-medium">Opened At</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-darkborder/50">
                {closedPositions.map((p) => {
                  const isProfitable = (p.realized_pnl_usd ?? 0) >= 0;

                  return (
                    <tr key={p.id} className="hover:bg-darkcard/50 transition">
                      <td className="py-3 px-3">
                        <div className="flex items-center space-x-1.5">
                          <span className="font-bold text-white">{p.symbol}</span>
                          <span
                            className={`text-[10px] font-mono px-1.5 py-0.5 rounded font-bold uppercase tracking-wider ${
                              (p.strategy_abbr || '').includes('DCA')
                                ? 'bg-purple-500/20 text-purple-300 border border-purple-500/30'
                                : (p.strategy_abbr || '').includes('EMA')
                                ? 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30'
                                : 'bg-amber-500/20 text-amber-300 border border-amber-500/30'
                            }`}
                            title={p.strategy_name || 'Strategy'}
                          >
                            {p.strategy_abbr || 'STRAT'}
                          </span>
                        </div>
                        <span className="text-[10px] text-gray-400 block truncate max-w-[130px]" title={p.strategy_name || ''}>
                          {p.strategy_name || p.market_type.replace('_', ' ')}
                        </span>
                      </td>

                      <td className="py-3 px-3">
                        <span className="font-bold text-gray-300">{p.side} {p.leverage}x</span>
                      </td>

                      <td className="py-3 px-3 text-gray-300">
                        ${Number(p.entry_price).toFixed(2)}
                      </td>

                      <td className="py-3 px-3">
                        <span className={`font-bold ${isProfitable ? 'text-emerald-400' : 'text-rose-400'}`}>
                          {isProfitable ? '+' : ''}${Number(p.realized_pnl_usd).toFixed(2)}
                        </span>
                      </td>

                      <td className="py-3 px-3">
                        <span className="px-1.5 py-0.5 rounded text-[10px] bg-darkcard border border-darkborder text-gray-300 font-semibold">
                          {p.exit_reason || 'CLOSED'}
                        </span>
                      </td>

                      <td className="py-3 px-3 text-gray-400 text-[11px]">
                        {new Date(p.opened_at).toLocaleTimeString()}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )
      )}
    </div>
  );
};

export default PositionsTable;
