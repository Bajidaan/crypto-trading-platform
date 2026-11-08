import React, { useState } from 'react';
import { Cpu, Play, Power, Settings2, Sliders, X } from 'lucide-react';
import type { StrategyItem } from '../types';

interface StrategyPanelProps {
  strategies: StrategyItem[];
  onToggleActive: (id: string, current: boolean) => void;
  onRunBacktest: (id: string) => void;
  onUpdateStrategy?: (id: string, updates: any) => Promise<void>;
  backtestResult: any;
}

const AVAILABLE_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d'];

const STRATEGY_DEFAULTS: Record<string, Record<string, number>> = {
  smart_dca: {
    base_order_size_usd: 150,
    base_entry_rsi_threshold: 35,
    rsi_period: 14,
    tp_target_pct: 2.0,
    step_drop_pct: 2.5,
    step_multiplier: 1.2,
    max_safety_orders: 3,
    hard_stop_loss_pct: 8.0,
  },
  ema_rsi_atr: {
    fast_ema_period: 9,
    slow_ema_period: 21,
    rsi_period: 14,
    rsi_long_threshold: 55,
    rsi_short_threshold: 45,
    atr_period: 14,
    atr_sl_multiplier: 2.5,
    tp1_r_multiplier: 1.5,
    tp2_r_multiplier: 3.0,
  },
  bb_breakout: {
    bb_period: 20,
    bb_std: 2,
    adx_period: 14,
    adx_threshold: 25,
    atr_period: 14,
    atr_sl_multiplier: 1.5,
    tp_r_multiplier: 2.5,
  },
  bollinger_breakout: {
    bb_period: 20,
    bb_std: 2,
    adx_period: 14,
    adx_threshold: 25,
    atr_period: 14,
    atr_sl_multiplier: 1.5,
    tp_r_multiplier: 2.5,
  },
};

export const StrategyPanel: React.FC<StrategyPanelProps> = ({
  strategies,
  onToggleActive,
  onRunBacktest,
  onUpdateStrategy,
  backtestResult,
}) => {
  const [editingStrategy, setEditingStrategy] = useState<StrategyItem | null>(null);
  const [editTimeframe, setEditTimeframe] = useState<string>('15m');
  const [editMode, setEditMode] = useState<'PAPER' | 'LIVE'>('PAPER');
  const [editMarketType, setEditMarketType] = useState<string>('FUTURES_USDT');
  const [editParams, setEditParams] = useState<Record<string, any>>({});
  const [isSaving, setIsSaving] = useState<boolean>(false);

  const openEditor = (s: StrategyItem) => {
    setEditingStrategy(s);
    setEditTimeframe(s.timeframe || '15m');
    setEditMode(s.execution_mode || 'PAPER');
    setEditMarketType(s.market_type || 'FUTURES_USDT');
    const defaults = STRATEGY_DEFAULTS[s.strategy_class] || {};
    setEditParams({ ...defaults, ...(s.parameters || {}) });
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingStrategy || !onUpdateStrategy) return;

    setIsSaving(true);
    try {
      await onUpdateStrategy(editingStrategy.id, {
        timeframe: editTimeframe,
        execution_mode: editMode,
        market_type: editMarketType,
        parameters: editParams,
      });
      setEditingStrategy(null);
    } catch (err: any) {
      alert(`Failed to save strategy: ${err.message}`);
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="bg-darkpanel border border-darkborder rounded-xl p-4 shadow-lg flex flex-col">
      <div className="flex items-center justify-between pb-3 mb-3 border-b border-darkborder">
        <div className="flex items-center space-x-2">
          <Cpu className="w-4 h-4 text-blue-400" />
          <h2 className="text-sm font-bold text-white uppercase tracking-wider">
            Trading Strategies ({strategies.length})
          </h2>
        </div>
        <span className="text-xs text-gray-400">
          Built-in Algorithmic Engines & Webhooks
        </span>
      </div>

      <div className="flex flex-col space-y-3">
        {(strategies || []).map((s) => (
          <div
            key={s.id}
            className={`p-3.5 rounded-lg border transition ${
              s.is_active
                ? 'bg-darkcard border-blue-500/30'
                : 'bg-darkcard/40 border-darkborder opacity-70'
            }`}
          >
            <div className="flex items-start justify-between">
              <div>
                <h3 className="text-xs font-bold text-white flex items-center space-x-1.5">
                  <span>{s.name}</span>
                </h3>
                <span className="text-[10px] font-mono text-gray-400">
                  {s.strategy_class} • <span className="text-blue-400 font-bold">{s.timeframe}</span>
                </span>
              </div>

              {/* Action Buttons: Configure & Toggle */}
              <div className="flex items-center space-x-1.5">
                <button
                  onClick={() => openEditor(s)}
                  className="p-1.5 rounded-md border border-darkborder bg-darkpanel hover:bg-darkborder text-gray-300 hover:text-white text-xs transition"
                  title="Configure Parameters & Timeframe"
                >
                  <Settings2 className="w-3.5 h-3.5" />
                </button>

                <button
                  onClick={() => onToggleActive(s.id, s.is_active)}
                  className={`p-1.5 rounded-md border text-xs transition ${
                    s.is_active
                      ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30'
                      : 'bg-gray-800 text-gray-400 border-darkborder'
                  }`}
                  title={s.is_active ? 'Click to Pause Strategy' : 'Click to Activate Strategy'}
                >
                  <Power className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>

            {/* Symbols */}
            <div className="mt-2 flex flex-wrap gap-1">
              {((s && s.symbols) || []).map((sym) => (
                <span
                  key={sym}
                  className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-darkpanel text-gray-300 border border-darkborder"
                >
                  {sym}
                </span>
              ))}
            </div>

            {/* Parameter snippets */}
            <div className="mt-2 text-[10px] font-mono text-gray-400 bg-darkpanel/60 p-2 rounded border border-darkborder/50 space-y-1">
              {Object.entries(s.parameters || {})
                .slice(0, 4)
                .map(([k, v]) => (
                  <div key={k} className="flex justify-between items-center">
                    <span className="text-gray-400 truncate max-w-[150px]">{k}:</span>
                    <span className="text-gray-200 font-semibold">{String(v)}</span>
                  </div>
                ))}
            </div>

            {/* Market Type, Mode & Backtest Button */}
            <div className="mt-3 flex items-center justify-between pt-1">
              <div className="flex items-center space-x-1.5">
                <span
                  className={`text-[10px] font-semibold uppercase px-2 py-0.5 rounded ${
                    s.execution_mode === 'LIVE'
                      ? 'bg-rose-500/20 text-rose-300 border border-rose-500/30'
                      : 'bg-blue-500/20 text-blue-300 border border-blue-500/30'
                  }`}
                >
                  {s.execution_mode}
                </span>

                <span
                  className={`text-[10px] font-mono font-semibold uppercase px-2 py-0.5 rounded border ${
                    s.market_type === 'SPOT'
                      ? 'bg-amber-500/20 text-amber-300 border-amber-500/30'
                      : 'bg-purple-500/20 text-purple-300 border-purple-500/30'
                  }`}
                >
                  {s.market_type === 'SPOT' ? 'SPOT (1x)' : 'FUTURES (USDT-M)'}
                </span>
              </div>

              <button
                onClick={() => onRunBacktest(s.id)}
                className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-blue-600/20 hover:bg-blue-600/40 text-blue-300 border border-blue-500/30 text-xs font-semibold transition"
              >
                <Play className="w-3 h-3" />
                <span>Run Backtest</span>
              </button>
            </div>
          </div>
        ))}
      </div>

      {/* Edit Strategy Modal */}
      {editingStrategy && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4">
          <div className="bg-darkpanel border border-darkborder rounded-xl max-w-lg w-full p-6 shadow-2xl">
            <div className="flex items-center justify-between pb-3 border-b border-darkborder mb-4">
              <div className="flex items-center space-x-2 text-white">
                <Settings2 className="w-5 h-5 text-blue-400" />
                <h3 className="font-bold text-sm">Configure {editingStrategy.name}</h3>
              </div>
              <button
                onClick={() => setEditingStrategy(null)}
                className="p-1 rounded text-gray-400 hover:text-white"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <form onSubmit={handleSave} className="space-y-4 text-xs">
              {/* Market Type, Timeframe & Mode */}
              <div className="grid grid-cols-3 gap-2.5">
                <div>
                  <label className="block text-gray-400 mb-1 font-semibold">Market Type</label>
                  <select
                    value={editMarketType}
                    onChange={(e) => setEditMarketType(e.target.value)}
                    className="w-full bg-darkcard border border-darkborder rounded-lg px-2.5 py-2 text-white font-mono focus:border-blue-500 outline-none text-[11px]"
                  >
                    <option value="FUTURES_USDT">USDT-M Futures</option>
                    <option value="SPOT">Spot Exchange</option>
                  </select>
                </div>

                <div>
                  <label className="block text-gray-400 mb-1 font-semibold">Candle TF</label>
                  <select
                    value={editTimeframe}
                    onChange={(e) => setEditTimeframe(e.target.value)}
                    className="w-full bg-darkcard border border-darkborder rounded-lg px-2.5 py-2 text-white font-mono focus:border-blue-500 outline-none text-[11px]"
                  >
                    {AVAILABLE_TIMEFRAMES.map((tf) => (
                      <option key={tf} value={tf}>
                        {tf}
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="block text-gray-400 mb-1 font-semibold">Execution</label>
                  <select
                    value={editMode}
                    onChange={(e) => setEditMode(e.target.value as any)}
                    className="w-full bg-darkcard border border-darkborder rounded-lg px-2.5 py-2 text-white font-mono focus:border-blue-500 outline-none text-[11px]"
                  >
                    <option value="PAPER">PAPER (Sim)</option>
                    <option value="LIVE">LIVE (Real)</option>
                  </select>
                </div>
              </div>

              {/* Strategy Parameters Editor */}
              <div>
                <label className="block text-gray-400 mb-2 font-semibold uppercase tracking-wider text-[11px]">
                  Strategy Mathematical Parameters
                </label>
                <div className="space-y-2 max-h-56 overflow-y-auto pr-1">
                  {Object.entries(editParams).map(([paramKey, paramVal]) => (
                    <div key={paramKey} className="flex items-center justify-between bg-darkcard p-2 rounded border border-darkborder">
                      <span className="font-mono text-gray-300 text-[11px] truncate max-w-[200px]">
                        {paramKey}
                      </span>
                      <input
                        type="number"
                        step="any"
                        value={paramVal}
                        onChange={(e) =>
                          setEditParams({
                            ...editParams,
                            [paramKey]: e.target.value === '' ? '' : Number(e.target.value),
                          })
                        }
                        className="w-24 bg-darkpanel border border-darkborder rounded px-2 py-1 text-right text-white font-mono text-[11px] focus:border-blue-500 outline-none"
                      />
                    </div>
                  ))}
                </div>
              </div>

              {/* Save & Cancel */}
              <div className="flex justify-end space-x-2 pt-3 border-t border-darkborder">
                <button
                  type="button"
                  onClick={() => setEditingStrategy(null)}
                  className="px-3 py-1.5 rounded bg-darkcard text-gray-300 hover:bg-darkborder text-xs"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={isSaving}
                  className="px-4 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white font-semibold text-xs shadow transition disabled:opacity-50"
                >
                  {isSaving ? 'Saving...' : 'Save Strategy'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Backtest Results Modal / Banner */}
      {backtestResult && (
        <div className="mt-4 p-3 bg-darkcard border border-blue-500/40 rounded-lg">
          <div className="flex items-center justify-between pb-2 mb-2 border-b border-darkborder text-xs">
            <span className="font-bold text-white flex items-center space-x-1">
              <Sliders className="w-3.5 h-3.5 text-blue-400" />
              <span>Backtest Performance Summary ({backtestResult.symbol || 'BTC/USDT'})</span>
            </span>
            <span className="font-mono text-gray-400 text-[10px]">
              {backtestResult.total_trades || 0} Trades Executed
            </span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs font-mono">
            <div className="bg-darkpanel p-2 rounded border border-darkborder">
              <div className="text-[10px] text-gray-400">Total Return</div>
              <div
                className={`text-sm font-bold ${
                  Number(backtestResult.total_return_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'
                }`}
              >
                {Number(backtestResult.total_return_pct ?? 0) >= 0 ? '+' : ''}
                {Number(backtestResult.total_return_pct ?? 0).toFixed(2)}%
              </div>
            </div>

            <div className="bg-darkpanel p-2 rounded border border-darkborder">
              <div className="text-[10px] text-gray-400">Win Rate</div>
              <div className="text-sm font-bold text-white">
                {Number(backtestResult.win_rate_pct ?? 0).toFixed(1)}%
              </div>
            </div>

            <div className="bg-darkpanel p-2 rounded border border-darkborder">
              <div className="text-[10px] text-gray-400">Profit Factor</div>
              <div className="text-sm font-bold text-blue-400">
                {Number(backtestResult.profit_factor ?? 0).toFixed(2)}
              </div>
            </div>

            <div className="bg-darkpanel p-2 rounded border border-darkborder">
              <div className="text-[10px] text-gray-400">Max Drawdown</div>
              <div className="text-sm font-bold text-rose-400">
                {Number(backtestResult.max_drawdown_pct ?? 0).toFixed(2)}%
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
