import React, { useState } from 'react';
import {
  Activity,
  AlertOctagon,
  AlertTriangle,
  BarChart3,
  FlaskConical,
  Key,
  PauseCircle,
  PlayCircle,
  ShieldAlert,
  Wifi,
  WifiOff,
} from 'lucide-react';
import type { SystemStatus, TradingEnvironment } from '../types';

interface HeaderProps {
  status: SystemStatus | null;
  wsStatus: 'CONNECTED' | 'CONNECTING' | 'DISCONNECTED';
  onPause: () => void;
  onResume: () => void;
  onKillSwitch: () => void;
  onOpenConnections: () => void;
  onOpenAnalytics: () => void;
  onSwitchEnvironment: (env: TradingEnvironment) => Promise<void>;
}

const ENV_STYLE: Record<TradingEnvironment, { label: string; active: string; text: string }> = {
  DEMO: {
    label: 'Demo',
    active: 'bg-amber-500 text-black',
    text: 'text-amber-300',
  },
  LIVE: {
    label: 'Live',
    active: 'bg-rose-600 text-white',
    text: 'text-rose-300',
  },
};

export const Header: React.FC<HeaderProps> = ({
  status,
  wsStatus,
  onPause,
  onResume,
  onKillSwitch,
  onOpenConnections,
  onOpenAnalytics,
  onSwitchEnvironment,
}) => {
  const [showKillConfirm, setShowKillConfirm] = useState(false);
  const [showLiveConfirm, setShowLiveConfirm] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [switchError, setSwitchError] = useState<string | null>(null);

  const isPaused = status?.is_paused ?? false;
  const isKillSwitch = status?.kill_switch_active ?? false;
  const env: TradingEnvironment = status?.environment ?? 'DEMO';
  const envStyle = ENV_STYLE[env];
  const equity = status?.equity_usd ?? null;
  const openPositions = status?.open_positions ?? 0;
  const otherOpen = status?.other_environment_open_positions ?? 0;
  const maxPositions = status?.max_concurrent_positions ?? 5;
  const maxDrawdown = status?.max_daily_drawdown_pct ?? 0;
  const otherEnv: TradingEnvironment = env === 'DEMO' ? 'LIVE' : 'DEMO';

  const doSwitch = async (target: TradingEnvironment) => {
    setSwitching(true);
    setSwitchError(null);
    try {
      await onSwitchEnvironment(target);
      setShowLiveConfirm(false);
    } catch (err: any) {
      setSwitchError(err.message || 'Switch failed');
    } finally {
      setSwitching(false);
    }
  };

  const requestSwitch = (target: TradingEnvironment) => {
    if (target === env || switching) return;
    setSwitchError(null);
    if (target === 'LIVE') {
      setShowLiveConfirm(true);
    } else {
      void doSwitch(target);
    }
  };

  let equityHint = status?.connection_label ?? 'No API key';
  if (status?.connection_label && status.balance_error && equity === null) {
    equityHint = 'Balance unavailable';
  }

  return (
    <header className="bg-darkpanel border-b border-darkborder px-6 py-3 flex flex-wrap items-center justify-between gap-4 sticky top-0 z-40">
      {/* Brand */}
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
          <p className="text-xs text-gray-400">Binance Spot / USDT-M Futures</p>
        </div>
      </div>

      {/* Environment switch */}
      <div className="flex flex-col items-start">
        <span className="text-[11px] text-gray-400 uppercase font-medium mb-1">Trading account</span>
        <div
          role="radiogroup"
          aria-label="Trading environment"
          className="inline-flex rounded-lg border border-darkborder bg-darkcard p-0.5"
        >
          {(['DEMO', 'LIVE'] as TradingEnvironment[]).map((e) => (
            <button
              key={e}
              role="radio"
              aria-checked={env === e}
              disabled={switching}
              onClick={() => requestSwitch(e)}
              className={`flex items-center space-x-1.5 px-3 py-1 rounded-md text-xs font-bold transition ${
                env === e ? ENV_STYLE[e].active : 'text-gray-400 hover:text-white'
              } disabled:opacity-60`}
            >
              {e === 'DEMO' ? <FlaskConical className="w-3.5 h-3.5" /> : <AlertTriangle className="w-3.5 h-3.5" />}
              <span>{ENV_STYLE[e].label}</span>
            </button>
          ))}
        </div>
        {switchError && !showLiveConfirm && (
          <span className="mt-1 text-[11px] text-rose-400 max-w-[260px]">{switchError}</span>
        )}
        {otherOpen > 0 && (
          <span className="mt-1 text-[11px] text-gray-400">
            {otherOpen} open on {ENV_STYLE[otherEnv].label}, still protected
          </span>
        )}
      </div>

      {/* Metrics Bar */}
      <div className="flex items-center space-x-6 text-sm">
        <div className="flex flex-col">
          <span className="text-[11px] text-gray-400 uppercase font-medium">{envStyle.label} Equity</span>
          <span className={`font-mono text-base font-bold ${equity === null ? 'text-gray-500' : envStyle.text}`}>
            {equity === null
              ? '—'
              : `$${equity.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
          </span>
          <span className="text-[10px] text-gray-500 truncate max-w-[160px]" title={status?.balance_error ?? undefined}>
            {equityHint}
          </span>
        </div>

        <div className="flex flex-col">
          <span className="text-[11px] text-gray-400 uppercase font-medium">Open Positions</span>
          <span className="font-mono text-base font-semibold text-gray-200">
            {openPositions} / <span className="text-gray-500">{maxPositions}</span>
          </span>
        </div>

        <div className="flex flex-col">
          <span className="text-[11px] text-gray-400 uppercase font-medium">Max Daily Drawdown</span>
          <span className="font-mono text-base font-semibold text-amber-400">
            {maxDrawdown.toFixed(2)}%
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
              <span className="w-2 h-2 rounded-full bg-emerald-400 mr-1.5 animate-pulse" /> ENGINE RUNNING
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

      {/* Switch-to-LIVE Confirmation Modal */}
      {showLiveConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-sm p-4">
          <div className="bg-darkpanel border border-rose-500/40 rounded-xl p-6 max-w-md w-full shadow-2xl">
            <div className="flex items-center space-x-3 text-rose-500 mb-4">
              <AlertTriangle className="w-7 h-7" />
              <h3 className="text-lg font-bold text-white">Trade with real money?</h3>
            </div>
            <p className="text-sm text-gray-300 leading-relaxed mb-3">
              Active strategies will place real orders on your Binance account from the next signal on.
            </p>
            <p className="text-xs text-gray-400 leading-relaxed mb-6">
              Positions already open on Demo stay open there and keep their stop-losses. You can switch
              back to Demo at any time.
            </p>
            {switchError && (
              <div className="mb-4 p-2.5 rounded bg-rose-500/20 border border-rose-500/30 text-rose-300 text-xs">
                {switchError}
              </div>
            )}
            <div className="flex justify-end space-x-3">
              <button
                onClick={() => {
                  setShowLiveConfirm(false);
                  setSwitchError(null);
                }}
                className="px-4 py-2 rounded bg-darkcard text-gray-300 hover:bg-darkborder text-xs font-medium"
              >
                Stay on Demo
              </button>
              <button
                disabled={switching}
                onClick={() => void doSwitch('LIVE')}
                className="px-4 py-2 rounded bg-rose-600 hover:bg-rose-500 text-white text-xs font-bold disabled:opacity-60"
              >
                {switching ? 'Switching...' : 'Switch to Live'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Kill Switch Confirmation Modal */}
      {showKillConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-sm p-4">
          <div className="bg-darkpanel border border-rose-500/40 rounded-xl p-6 max-w-md w-full shadow-2xl">
            <div className="flex items-center space-x-3 text-rose-500 mb-4">
              <ShieldAlert className="w-7 h-7" />
              <h3 className="text-lg font-bold text-white">Emergency Kill Switch</h3>
            </div>
            <p className="text-sm text-gray-300 leading-relaxed mb-3">
              The Kill Switch immediately blocks every new entry and halts the engine until you turn
              it off again.
            </p>
            <p className="text-xs text-amber-300 leading-relaxed mb-6">
              It does not close open positions. They keep their exchange stop-losses; close them from
              the positions table (or on Binance) if you want out now.
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
