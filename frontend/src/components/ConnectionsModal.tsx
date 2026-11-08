import React, { useState } from 'react';
import { Key, Plus, Shield, Trash2, X } from 'lucide-react';
import type { ConnectionItem } from '../types';

interface ConnectionsModalProps {
  isOpen: boolean;
  onClose: () => void;
  connections: ConnectionItem[];
  onAddConnection: (payload: {
    exchange_id: string;
    label: string;
    api_key: string;
    api_secret: string;
    is_testnet: boolean;
    spot_capital_cap_usd?: number;
    futures_capital_cap_usd?: number;
  }) => Promise<void>;
  onDeleteConnection: (id: string) => Promise<void>;
}

export const ConnectionsModal: React.FC<ConnectionsModalProps> = ({
  isOpen,
  onClose,
  connections,
  onAddConnection,
  onDeleteConnection,
}) => {
  const [exchangeId, setExchangeId] = useState('BINANCE');
  const [label, setLabel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [isTestnet, setIsTestnet] = useState(true);
  const [futuresCap, setFuturesCap] = useState('2000');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!label || !apiKey || !apiSecret) {
      setErrorMsg('Please complete all required fields.');
      return;
    }

    setIsSubmitting(true);
    setErrorMsg(null);
    try {
      await onAddConnection({
        exchange_id: exchangeId,
        label,
        api_key: apiKey,
        api_secret: apiSecret,
        is_testnet: isTestnet,
        futures_capital_cap_usd: Number(futuresCap) || 2000,
      });
      setLabel('');
      setApiKey('');
      setApiSecret('');
    } catch (err: any) {
      setErrorMsg(err.message || 'Failed to register connection.');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4">
      <div className="bg-darkpanel border border-darkborder rounded-xl max-w-2xl w-full p-6 shadow-2xl overflow-y-auto max-h-[90vh]">
        {/* Modal Header */}
        <div className="flex items-center justify-between pb-4 border-b border-darkborder">
          <div className="flex items-center space-x-2 text-white">
            <Key className="w-5 h-5 text-blue-400" />
            <h2 className="text-base font-bold">Exchange API Connections</h2>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded-lg text-gray-400 hover:text-white hover:bg-darkborder"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Security Notice */}
        <div className="my-4 p-3 bg-blue-950/20 border border-blue-500/30 rounded-lg flex items-start space-x-2 text-xs text-blue-300">
          <Shield className="w-4 h-4 text-blue-400 mt-0.5 flex-shrink-0" />
          <div>
            API secrets are encrypted at rest with hardware-derived AES-256-GCM. Decryption keys
            only exist in ephemeral RAM and are never written to disk or sent across the network.
          </div>
        </div>

        {/* Active Connections List */}
        <div className="mb-6">
          <h3 className="text-xs font-bold uppercase text-gray-400 tracking-wider mb-2">
            Configured Exchanges ({connections.length})
          </h3>
          {connections.length === 0 ? (
            <div className="p-4 bg-darkcard rounded-lg text-center text-xs text-gray-500">
              No API keys configured. Add your Binance or Bybit credentials below to enable live
              exchange order execution.
            </div>
          ) : (
            <div className="space-y-2">
              {connections.map((c) => (
                <div
                  key={c.id}
                  className="p-3 bg-darkcard border border-darkborder rounded-lg flex items-center justify-between text-xs"
                >
                  <div>
                    <div className="flex items-center space-x-2">
                      <span className="font-bold text-white">{c.label}</span>
                      <span className="text-[10px] uppercase font-bold px-1.5 py-0.5 rounded bg-darkpanel text-blue-400 border border-darkborder">
                        {c.exchange_id}
                      </span>
                      {c.is_testnet && (
                        <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30">
                          TESTNET
                        </span>
                      )}
                    </div>
                    <div className="text-gray-400 font-mono mt-1 text-[11px]">
                      Key: {c.api_key_masked} • Cap: ${c.futures_capital_cap_usd || 0}
                    </div>
                  </div>

                  <button
                    onClick={() => onDeleteConnection(c.id)}
                    className="p-1.5 rounded hover:bg-rose-500/20 text-gray-400 hover:text-rose-400 transition"
                    title="Remove Credential"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Add Connection Form */}
        <form onSubmit={handleSubmit} className="border-t border-darkborder pt-4">
          <h3 className="text-xs font-bold uppercase text-gray-400 tracking-wider mb-3">
            Add New Exchange API Key
          </h3>

          {errorMsg && (
            <div className="mb-3 p-2.5 rounded bg-rose-500/20 border border-rose-500/30 text-rose-300 text-xs">
              {errorMsg}
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs mb-3">
            <div>
              <label className="block text-gray-400 mb-1">Exchange</label>
              <select
                value={exchangeId}
                onChange={(e) => setExchangeId(e.target.value)}
                className="w-full bg-darkcard border border-darkborder rounded-lg px-3 py-2 text-white font-mono focus:border-blue-500 outline-none"
              >
                <option value="BINANCE">Binance (Spot + Futures)</option>
                <option value="BYBIT">Bybit (V5 Unified / Linear)</option>
              </select>
            </div>

            <div>
              <label className="block text-gray-400 mb-1">Connection Label</label>
              <input
                type="text"
                placeholder="e.g. Binance Main Futures"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                className="w-full bg-darkcard border border-darkborder rounded-lg px-3 py-2 text-white focus:border-blue-500 outline-none"
              />
            </div>

            <div>
              <label className="block text-gray-400 mb-1">API Key</label>
              <input
                type="text"
                placeholder="Paste API Key"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                className="w-full bg-darkcard border border-darkborder rounded-lg px-3 py-2 text-white font-mono focus:border-blue-500 outline-none"
              />
            </div>

            <div>
              <label className="block text-gray-400 mb-1">API Secret</label>
              <input
                type="password"
                placeholder="Paste API Secret"
                value={apiSecret}
                onChange={(e) => setApiSecret(e.target.value)}
                className="w-full bg-darkcard border border-darkborder rounded-lg px-3 py-2 text-white font-mono focus:border-blue-500 outline-none"
              />
            </div>

            <div>
              <label className="block text-gray-400 mb-1">Futures Capital Cap ($)</label>
              <input
                type="number"
                placeholder="2000"
                value={futuresCap}
                onChange={(e) => setFuturesCap(e.target.value)}
                className="w-full bg-darkcard border border-darkborder rounded-lg px-3 py-2 text-white font-mono focus:border-blue-500 outline-none"
              />
            </div>

            <div className="flex items-center pt-5">
              <label className="flex items-center space-x-2 cursor-pointer text-gray-300">
                <input
                  type="checkbox"
                  checked={isTestnet}
                  onChange={(e) => setIsTestnet(e.target.checked)}
                  className="rounded bg-darkcard border-darkborder text-blue-600 focus:ring-0"
                />
                <span className="font-semibold">Testnet / Sandbox Mode</span>
              </label>
            </div>
          </div>

          <div className="flex justify-end space-x-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 rounded bg-darkcard hover:bg-darkborder text-gray-300 text-xs font-medium"
            >
              Close
            </button>
            <button
              type="submit"
              disabled={isSubmitting}
              className="flex items-center space-x-1.5 px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white text-xs font-bold transition disabled:opacity-50"
            >
              <Plus className="w-4 h-4" />
              <span>{isSubmitting ? 'Encrypting & Storing...' : 'Save Connection'}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
