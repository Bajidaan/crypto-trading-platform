import React, { useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, FlaskConical, Key, Plus, Shield, Trash2, X } from 'lucide-react';
import type { ConnectionItem, TradingEnvironment } from '../types';

interface ConnectionsModalProps {
  isOpen: boolean;
  onClose: () => void;
  connections: ConnectionItem[];
  // The account the header switch is on; used to pick the form's default.
  activeEnvironment: TradingEnvironment;
  onAddConnection: (payload: {
    exchange_id: string;
    label: string;
    api_key: string;
    api_secret: string;
    environment: TradingEnvironment;
    spot_capital_cap_usd?: number;
    futures_capital_cap_usd?: number;
  }) => Promise<ConnectionItem>;
  onDeleteConnection: (id: string) => Promise<void>;
}

const ACCOUNTS: Record<
  TradingEnvironment,
  { title: string; short: string; badge: string; where: React.ReactNode; empty: string }
> = {
  DEMO: {
    title: 'Demo Trading',
    short: 'Demo',
    badge: 'bg-amber-500/20 text-amber-300 border-amber-500/30',
    where: (
      <>
        Create it on{' '}
        <a href="https://demo.binance.com" target="_blank" rel="noreferrer" className="text-blue-400 hover:underline">
          demo.binance.com
        </a>{' '}
        → API Management. Demo funds only; keys from your real account do not work here.
      </>
    ),
    empty: 'No Demo key yet. Strategies cannot trade while the app is on Demo.',
  },
  LIVE: {
    title: 'Live',
    short: 'Live',
    badge: 'bg-rose-500/20 text-rose-300 border-rose-500/30',
    where: (
      <>
        Create it on{' '}
        <a
          href="https://www.binance.com/en/my/settings/api-management"
          target="_blank"
          rel="noreferrer"
          className="text-blue-400 hover:underline"
        >
          binance.com → API Management
        </a>
        . Allow Reading and Spot &amp; Futures trading. Never allow withdrawals; an IP restriction is
        recommended.
      </>
    ),
    empty: 'No Live key yet. Add one before switching the app to Live.',
  },
};

const capText = (cap: number | null) => (cap === null || cap === 0 ? 'no cap' : `$${cap.toLocaleString('en-US')}`);

export const ConnectionsModal: React.FC<ConnectionsModalProps> = ({
  isOpen,
  onClose,
  connections,
  activeEnvironment,
  onAddConnection,
  onDeleteConnection,
}) => {
  const [environment, setEnvironment] = useState<TradingEnvironment>('DEMO');
  const [label, setLabel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [futuresCap, setFuturesCap] = useState('2000');
  const [spotCap, setSpotCap] = useState('1000');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [saved, setSaved] = useState<ConnectionItem | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // Default the form to the account that still has no key, else the active one.
  useEffect(() => {
    if (!isOpen) return;
    const hasKey = (env: TradingEnvironment) => connections.some((c) => c.environment === env);
    if (!hasKey(activeEnvironment)) setEnvironment(activeEnvironment);
    else if (!hasKey('DEMO')) setEnvironment('DEMO');
    else if (!hasKey('LIVE')) setEnvironment('LIVE');
    else setEnvironment(activeEnvironment);
    setErrorMsg(null);
    setSaved(null);
    setDeleteError(null);
    // Only when the dialog opens; switching accounts in the form is the user's choice.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!apiKey.trim() || !apiSecret.trim()) {
      setErrorMsg('Paste both the API key and the secret.');
      return;
    }
    setIsSubmitting(true);
    setErrorMsg(null);
    setSaved(null);
    try {
      const conn = await onAddConnection({
        exchange_id: 'BINANCE',
        label,
        api_key: apiKey,
        api_secret: apiSecret,
        environment,
        futures_capital_cap_usd: futuresCap === '' ? undefined : Number(futuresCap),
        spot_capital_cap_usd: spotCap === '' ? undefined : Number(spotCap),
      });
      setSaved(conn);
      setLabel('');
      setApiKey('');
      setApiSecret('');
    } catch (err: any) {
      setErrorMsg(err.message || 'Could not save this key.');
    } finally {
      setIsSubmitting(false);
    }
  };

  const renderAccount = (env: TradingEnvironment) => {
    const meta = ACCOUNTS[env];
    const keys = connections.filter((c) => (c.environment ?? (c.is_testnet ? 'DEMO' : 'LIVE')) === env);
    return (
      <section key={env} className="mb-4">
        <div className="flex items-center justify-between mb-1.5">
          <h3 className="text-xs font-bold uppercase tracking-wider text-gray-300 flex items-center space-x-1.5">
            {env === 'DEMO' ? (
              <FlaskConical className="w-3.5 h-3.5 text-amber-400" />
            ) : (
              <AlertTriangle className="w-3.5 h-3.5 text-rose-400" />
            )}
            <span>{meta.title}</span>
            {env === activeEnvironment && (
              <span className="text-[10px] font-semibold normal-case tracking-normal text-gray-400">
                · app is trading here
              </span>
            )}
          </h3>
        </div>
        {keys.length === 0 ? (
          <div className="p-3 bg-darkcard rounded-lg text-xs text-gray-500 border border-dashed border-darkborder">
            {meta.empty}
          </div>
        ) : (
          <div className="space-y-2">
            {keys.map((c) => (
              <div
                key={c.id}
                className="p-3 bg-darkcard border border-darkborder rounded-lg flex items-center justify-between text-xs"
              >
                <div>
                  <div className="flex items-center space-x-2">
                    <span className="font-bold text-white">{c.label}</span>
                    <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded border ${meta.badge}`}>
                      {meta.short.toUpperCase()}
                    </span>
                    {c.in_use ? (
                      <span className="text-[10px] font-bold px-1.5 py-0.5 rounded border bg-emerald-500/15 text-emerald-300 border-emerald-500/30">
                        IN USE
                      </span>
                    ) : (
                      <span className="text-[10px] text-gray-500" title="The newest key of an account is the one used">
                        not used (a newer {meta.short} key is in use)
                      </span>
                    )}
                  </div>
                  <div className="text-gray-400 font-mono mt-1 text-[11px]">
                    Key {c.api_key_masked} · Futures cap {capText(c.futures_capital_cap_usd)} · Spot cap{' '}
                    {capText(c.spot_capital_cap_usd)}
                  </div>
                </div>

                <button
                  onClick={async () => {
                    setDeleteError(null);
                    try {
                      await onDeleteConnection(c.id);
                    } catch (err: any) {
                      setDeleteError(err.message || 'Could not remove this API key.');
                    }
                  }}
                  className="p-1.5 rounded hover:bg-rose-500/20 text-gray-400 hover:text-rose-400 transition"
                  title="Remove this key"
                  aria-label={`Remove ${c.label}`}
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            ))}
          </div>
        )}
      </section>
    );
  };

  const meta = ACCOUNTS[environment];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4">
      <div className="bg-darkpanel border border-darkborder rounded-xl max-w-2xl w-full p-6 shadow-2xl overflow-y-auto max-h-[90vh]">
        {/* Modal Header */}
        <div className="flex items-center justify-between pb-4 border-b border-darkborder">
          <div className="flex items-center space-x-2 text-white">
            <Key className="w-5 h-5 text-blue-400" />
            <h2 className="text-base font-bold">Binance API Keys</h2>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="p-1 rounded-lg text-gray-400 hover:text-white hover:bg-darkborder"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <p className="mt-3 text-xs text-gray-400 leading-relaxed">
          Add one key for each Binance account. The <span className="text-gray-200 font-semibold">Demo | Live</span>{' '}
          switch at the top of the dashboard chooses which account the strategies trade on.
        </p>

        {/* Security Notice */}
        <div className="my-4 p-3 bg-blue-950/20 border border-blue-500/30 rounded-lg flex items-start space-x-2 text-xs text-blue-300">
          <Shield className="w-4 h-4 text-blue-400 mt-0.5 flex-shrink-0" />
          <div>
            Keys are encrypted (AES-256-GCM) with a secret kept only on this computer, in{' '}
            <span className="font-mono">vault.key</span> next to the database. Keep that file private and
            back it up: without it, saved keys can't be read.
          </div>
        </div>

        {deleteError && (
          <div className="mb-3 p-2.5 rounded bg-rose-500/20 border border-rose-500/30 text-rose-300 text-xs">
            {deleteError}
          </div>
        )}
        {renderAccount('DEMO')}
        {renderAccount('LIVE')}

        {/* Add Key Form */}
        <form onSubmit={handleSubmit} className="border-t border-darkborder pt-4 mt-2">
          <h3 className="text-xs font-bold uppercase text-gray-400 tracking-wider mb-3">Add a key</h3>

          <div className="mb-3">
            <span className="block text-gray-400 mb-1 text-xs">Account</span>
            <div role="radiogroup" aria-label="Account" className="inline-flex rounded-lg border border-darkborder bg-darkcard p-0.5">
              {(['DEMO', 'LIVE'] as TradingEnvironment[]).map((env) => (
                <button
                  key={env}
                  type="button"
                  role="radio"
                  aria-checked={environment === env}
                  onClick={() => {
                    setEnvironment(env);
                    setErrorMsg(null);
                  }}
                  className={`px-3 py-1 rounded-md text-xs font-bold transition ${
                    environment === env
                      ? env === 'DEMO'
                        ? 'bg-amber-500 text-black'
                        : 'bg-rose-600 text-white'
                      : 'text-gray-400 hover:text-white'
                  }`}
                >
                  {env === 'DEMO' ? 'Demo Trading (demo funds)' : 'Live (real money)'}
                </button>
              ))}
            </div>
            <p className="mt-1.5 text-[11px] text-gray-400 leading-relaxed">{meta.where}</p>
          </div>

          {errorMsg && (
            <div className="mb-3 p-2.5 rounded bg-rose-500/20 border border-rose-500/30 text-rose-300 text-xs">
              {errorMsg}
            </div>
          )}
          {saved && (
            <div className="mb-3 p-2.5 rounded bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 text-xs space-y-1">
              <div className="flex items-center space-x-1.5 font-semibold">
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>
                  Binance accepted “{saved.label}” for {ACCOUNTS[saved.environment].title}.
                  {saved.balances?.FUTURES_USDT !== undefined &&
                    ` Futures balance: $${saved.balances.FUTURES_USDT.toLocaleString('en-US', { maximumFractionDigits: 2 })}.`}
                </span>
              </div>
              {(saved.warnings ?? []).map((w) => (
                <div key={w} className="text-amber-300">{w}</div>
              ))}
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs mb-3">
            <div>
              <label htmlFor="key-api" className="block text-gray-400 mb-1">API Key</label>
              <input
                id="key-api"
                type="text"
                autoComplete="off"
                spellCheck={false}
                placeholder="Paste API key"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                className="w-full bg-darkcard border border-darkborder rounded-lg px-3 py-2 text-white font-mono focus:border-blue-500 outline-none"
              />
            </div>

            <div>
              <label htmlFor="key-secret" className="block text-gray-400 mb-1">API Secret</label>
              <input
                id="key-secret"
                type="password"
                autoComplete="new-password"
                placeholder="Paste API secret"
                value={apiSecret}
                onChange={(e) => setApiSecret(e.target.value)}
                className="w-full bg-darkcard border border-darkborder rounded-lg px-3 py-2 text-white font-mono focus:border-blue-500 outline-none"
              />
            </div>

            <div>
              <label htmlFor="key-label" className="block text-gray-400 mb-1">Name (optional)</label>
              <input
                id="key-label"
                type="text"
                placeholder={environment === 'DEMO' ? 'Binance Demo' : 'Binance Live'}
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                className="w-full bg-darkcard border border-darkborder rounded-lg px-3 py-2 text-white focus:border-blue-500 outline-none"
              />
            </div>

            <div className="grid grid-cols-2 gap-2">
              <div>
                <label htmlFor="key-futures-cap" className="block text-gray-400 mb-1">Futures cap ($)</label>
                <input
                  id="key-futures-cap"
                  type="number"
                  min={0}
                  value={futuresCap}
                  onChange={(e) => setFuturesCap(e.target.value)}
                  className="w-full bg-darkcard border border-darkborder rounded-lg px-3 py-2 text-white font-mono focus:border-blue-500 outline-none"
                />
              </div>
              <div>
                <label htmlFor="key-spot-cap" className="block text-gray-400 mb-1">Spot cap ($)</label>
                <input
                  id="key-spot-cap"
                  type="number"
                  min={0}
                  value={spotCap}
                  onChange={(e) => setSpotCap(e.target.value)}
                  className="w-full bg-darkcard border border-darkborder rounded-lg px-3 py-2 text-white font-mono focus:border-blue-500 outline-none"
                />
              </div>
            </div>
          </div>
          <p className="text-[11px] text-gray-500 mb-3">
            Caps limit how much of the balance strategies size orders from; 0 means no cap. The key is
            checked with Binance before it is saved.
          </p>

          <div className="flex justify-end space-x-2 pt-1">
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
              className={`flex items-center space-x-1.5 px-4 py-2 rounded text-white text-xs font-bold transition disabled:opacity-50 ${
                environment === 'LIVE' ? 'bg-rose-600 hover:bg-rose-500' : 'bg-blue-600 hover:bg-blue-500'
              }`}
            >
              <Plus className="w-4 h-4" />
              <span>
                {isSubmitting ? 'Checking with Binance…' : `Check & save ${ACCOUNTS[environment].short} key`}
              </span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
