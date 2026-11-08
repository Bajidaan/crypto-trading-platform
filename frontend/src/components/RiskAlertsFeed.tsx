import React from 'react';
import { AlertCircle, AlertTriangle, Bell, Info } from 'lucide-react';
import type { RiskAlertItem } from '../types';

interface RiskAlertsFeedProps {
  alerts: RiskAlertItem[];
}

export const RiskAlertsFeed: React.FC<RiskAlertsFeedProps> = ({ alerts }) => {
  return (
    <div className="bg-darkpanel border border-darkborder rounded-xl p-4 shadow-lg flex flex-col">
      <div className="flex items-center justify-between pb-3 mb-3 border-b border-darkborder">
        <div className="flex items-center space-x-2">
          <Bell className="w-4 h-4 text-amber-400" />
          <h2 className="text-sm font-bold text-white uppercase tracking-wider">
            Risk & System Events ({alerts.length})
          </h2>
        </div>
        <span className="text-xs text-gray-400">Live Audit & Circuit Breakers</span>
      </div>

      {alerts.length === 0 ? (
        <div className="py-6 text-center text-gray-500 text-xs">
          No risk alerts triggered. All safety circuit breakers nominal.
        </div>
      ) : (
        <div className="space-y-2 max-h-56 overflow-y-auto pr-1">
          {alerts.map((a) => {
            const isCritical = a.level === 'CRITICAL';
            const isWarning = a.level === 'WARNING';

            return (
              <div
                key={a.id}
                className={`p-2.5 rounded-lg border text-xs flex items-start space-x-2.5 transition ${
                  isCritical
                    ? 'bg-rose-950/30 border-rose-500/40 text-rose-200'
                    : isWarning
                    ? 'bg-amber-950/30 border-amber-500/40 text-amber-200'
                    : 'bg-darkcard border-darkborder text-gray-300'
                }`}
              >
                <div className="mt-0.5 flex-shrink-0">
                  {isCritical ? (
                    <AlertCircle className="w-4 h-4 text-rose-400" />
                  ) : isWarning ? (
                    <AlertTriangle className="w-4 h-4 text-amber-400" />
                  ) : (
                    <Info className="w-4 h-4 text-blue-400" />
                  )}
                </div>

                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between">
                    <span className="font-bold uppercase tracking-wider text-[10px]">
                      {a.level}
                    </span>
                    <span className="text-[10px] text-gray-500 font-mono">
                      {new Date(a.timestamp).toLocaleTimeString()}
                    </span>
                  </div>
                  <p className="mt-0.5 text-xs text-gray-200 font-mono leading-relaxed">
                    {a.reason}
                  </p>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
