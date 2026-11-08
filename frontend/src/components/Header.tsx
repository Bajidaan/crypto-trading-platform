import React, { useState } from 'react';
import {
  Activity,
  AlertOctagon,
  BarChart3,
  Key,
  PauseCircle,
  PlayCircle,
  ShieldAlert,
  Wifi,
  WifiOff,
} from 'lucide-react';
import type { SystemStatus } from '../types';

interface HeaderProps {
  status: SystemStatus | null;
  wsStatus: 'CONNECTED' | 'CONNECTING' | 'DISCONNECTED';
  onPause: () => void;
  onResume: () => void;
  onKillSwitch: () => void;
  onOpenConnections: () => void;
  onOpenAnalytics: () => void;
}

export const Header: React.FC<HeaderProps> = ({
  status,
  wsStatus,
  onPause,
  onResume,
  onKillSwitch,
  onOpenConnections,
  onOpenAnalytics,
}) => {
  const [showKillConfirm, setShowKillConfirm] = useState(false);

  const isPaused = status?.is_paused ?? false;
  const isKillSwitch = status?.kill_switch_active ?? false;
  const equity = status?.total_paper_equity ?? 10000;
  const openPositions = status?.open_paper_positions ?? 0;
  const maxPositions = status?.max_concurrent_positions ?? 5;
  const drawdown = status?.max_daily_drawdown_pct ?? 0;

  return (
    <header className="bg-darkpanel border-b border-darkborder px-6 py-3 flex flex-wrap items-center justify-between gap-4 sticky top-0 z-40">
      {/* Brand & Market Phase */}
      <div className="flex items-center space-x-3">
        <div className="bg-blue-600/20 text-blue-400 p-2 rounded-lg border border-blue-500/30">
          <Activity className="w-5 h-5" />
        </div>
        <div>
          <div className="flex items-center space-x-2">
            <span className="font-bold text-lg text-white tracking-wide">ANTIGRAVITY TRADER</span>
            <span className="text-[10px] uppercase font-semibold bg-blue-500/20 text-blue-400 border border-blue-500/30 px-2 py-0.5 rounded">
              Phase 1: CEX Engine
            </span>
          </div>
          <p className="text-xs text-gray-400">Binance & Bybit Spot / USDT-M Futures</p>
        </div>
      </div>

      {/* Metrics Bar */}
      <div className="flex items-center space-x-6 text-sm">
        <div className="flex flex-col">
          <span className="text-[11px] text-gray-400 uppercase font-medium">Paper Equity</span>
          <span className="font-mono text-base font-bold text-emerald-400">
            ${equity.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </span>
        </div>

        <div className="flex flex-col">
          <span className="text-[11px] text-gray-400 uppercase font-medium">Open Positions</span>
          <span className="font-mono text-base font-semibold text-gray-200">
            {openPositions} / <span className="text-gray-500">{maxPositions}</span>
          </span>
        </div>

        <div className="flex flex-col">
          <span className="text-[11px] text-gray-400 uppercase font-medium">Daily Drawdown</span>
          <span className="font-mono text-base font-semibold text-amber-400">
            {drawdown.toFixed(2)}%
          </span>
        </div>

        {/* Engine Status Badge */}
        <div className="flex items-center space-x-2 pl-2 border-l border-darkborder">
          {isKillSwitch ? (
            <span className="inline-flex items-center px-2.5 py-1 rounded text-xs font-bold bg-rose-500/20 text-rose-400 border border-rose-500/30 animate-pulse">
              <AlertOctagon className="w-3.5 h-3.5 mr-1" /> KILL SWITCH ACTIVE
            </span>
          ) : isPaused ? (
            <span className="inline-flex items-center px-2.5 py-1 rounded text-xs font-bold bg-amber-500/20 text-amber-400 border border-amber-500/30">
              <PauseCircle className="w-3.5 h-3.5 mr-1" /> ENGINE PAUSED
            </span>
          ) : (
            <span className="inline-flex items-center px-2.5 py-1 rounded text-xs font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">
              <span className="w-2 h-2 rounded-full bg-emerald-400 mr-1.5 animate-pulse" /> LIVE ENGINE
            </span>
          )}

          {/* WebSocket Status */}
          <div
            className={`flex items-center text-xs px-2 py-1 rounded border ${
              wsStatus === 'CONNECTED'
                ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
                : 'bg-rose-500/10 text-rose-400 border-rose-500/20'
            }`}
            title={`WebSocket: ${wsStatus}`}
          >
            {wsStatus === 'CONNECTED' ? (
              <Wifi className="w-3.5 h-3.5 mr-1" />
            ) : (
              <WifiOff className="w-3.5 h-3.5 mr-1" />
            )}
            <span className="font-mono">{wsStatus}</span>
          </div>
        </div>
      </div>

      {/* Control Buttons */}
      <div className="flex items-center space-x-2">
        <button
          onClick={onOpenConnections}
          className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-darkcard hover:bg-darkborder text-gray-200 border border-darkborder text-xs font-medium transition"
        >
          <Key className="w-3.5 h-3.5 text-blue-400" />
          <span>API Keys</span>
        </button>

        <button
          onClick={onOpenAnalytics}
          className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-blue-600/20 hover:bg-blue-600/30 text-blue-300 border border-blue-500/30 text-xs font-medium transition"
        >
          <BarChart3 className="w-3.5 h-3.5 text-blue-400" />
          <span>Analytics & Journal</span>
        </button>

        {isPaused ? (
          <button
            onClick={onResume}
            disabled={isKillSwitch}
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold shadow transition disabled:opacity-50"
          >
            <PlayCircle className="w-3.5 h-3.5" />
            <span>Resume</span>
          </button>
        ) : (
          <button
            onClick={onPause}
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-amber-600 hover:bg-amber-500 text-white text-xs font-semibold shadow transition"
          >
            <PauseCircle className="w-3.5 h-3.5" />
            <span>Pause</span>
          </button>
        )}

        <button
          onClick={() => setShowKillConfirm(true)}
          className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-rose-600 hover:bg-rose-500 text-white text-xs font-semibold shadow transition"
        >
          <ShieldAlert className="w-3.5 h-3.5" />
          <span>Kill Switch</span>
        </button>
      </div>

      {/* Kill Switch Confirmation Modal */}
      {showKillConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-sm p-4">
          <div className="bg-darkpanel border border-rose-500/40 rounded-xl p-6 max-w-md w-full shadow-2xl">
            <div className="flex items-center space-x-3 text-rose-500 mb-4">
              <ShieldAlert className="w-7 h-7" />
              <h3 className="text-lg font-bold text-white">Emergency Kill Switch</h3>
            </div>
            <p className="text-sm text-gray-300 leading-relaxed mb-6">
              Activating the Emergency Kill Switch immediately cancels all open exchange orders,
              market-closes all active positions, and locks the trading engine into a halted state.
              Manual intervention will be required to re-enable trading.
            </p>
            <div className="flex justify-end space-x-3">
              <button
                onClick={() => setShowKillConfirm(false)}
                className="px-4 py-2 rounded bg-darkcard text-gray-300 hover:bg-darkborder text-xs font-medium"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  setShowKillConfirm(false);
                  onKillSwitch();
                }}
                className="px-4 py-2 rounded bg-rose-600 hover:bg-rose-500 text-white text-xs font-bold"
              >
                Confirm & Halt System
              </button>
            </div>
          </div>
        </div>
      )}
    </header>
  );
};
