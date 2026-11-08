import React, { useEffect, useState } from 'react';
import { AnalyticsModal } from './components/AnalyticsModal';
import { ConnectionsModal } from './components/ConnectionsModal';
import { Header } from './components/Header';
import { OrdersTable } from './components/OrdersTable';
import { PositionsTable } from './components/PositionsTable';
import { RiskAlertsFeed } from './components/RiskAlertsFeed';
import { StrategyPanel } from './components/StrategyPanel';
import { TradingViewChart } from './components/TradingViewChart';
import { api } from './services/api';
import { wsService } from './services/websocket';
import type {
  ConnectionItem,
  OrderItem,
  PositionItem,
  RiskAlertItem,
  StrategyItem,
  SystemStatus,
} from './types';

export const App: React.FC = () => {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [wsStatus, setWsStatus] = useState<'CONNECTED' | 'CONNECTING' | 'DISCONNECTED'>('CONNECTING');
  const [symbol, setSymbol] = useState<string>('BTC/USDT');
  const [timeframe, setTimeframe] = useState<string>('15m');
  const [positions, setPositions] = useState<PositionItem[]>([]);
  const [orders, setOrders] = useState<OrderItem[]>([]);
  const [strategies, setStrategies] = useState<StrategyItem[]>([]);
  const [connections, setConnections] = useState<ConnectionItem[]>([]);
  const [alerts, setAlerts] = useState<RiskAlertItem[]>([]);
  const [backtestResult, setBacktestResult] = useState<any>(null);
  const [isConnectionsOpen, setIsConnectionsOpen] = useState<boolean>(false);
  const [isAnalyticsOpen, setIsAnalyticsOpen] = useState<boolean>(false);

  // Initial load
  const refreshAllData = async () => {
    try {
      const [sysStatus, posList, ordList, stratList, connList] = await Promise.all([
        api.getSystemStatus().catch((err) => {
          console.warn('Backend not responding yet for system status:', err);
          return {
            is_paused: false,
            kill_switch_active: false,
            max_concurrent_positions: 5,
            max_daily_drawdown_pct: 0,
            max_leverage_cap: 10,
            open_paper_positions: 1,
            total_paper_equity: 10000,
            active_stream_subscriptions: 1,
            event_bus_published_count: 0,
          };
        }),
        api.getPositions().catch(() => []),
        api.getOrders().catch(() => []),
        api.getStrategies().catch(() => []),
        api.getConnections().catch(() => []),
      ]);
      setStatus(sysStatus);
      setPositions(posList);
      setOrders(ordList);
      setStrategies(stratList);
      setConnections(connList);
    } catch (err) {
      console.error('Failed to load initial data:', err);
    }
  };

  useEffect(() => {
    refreshAllData();

    // Setup WebSocket
    wsService.connect();
    const unsubStatus = wsService.onStatusChange(setWsStatus);

    // Subscriptions
    const unsubPositions = wsService.subscribe('position', (msg) => {
      if (msg.data) {
        setPositions((prev) => {
          const idx = prev.findIndex((p) => p.id === msg.data.id);
          if (idx >= 0) {
            const next = [...prev];
            next[idx] = { ...next[idx], ...msg.data };
            return next;
          }
          return [msg.data, ...prev];
        });
      }
    });

    const unsubOrders = wsService.subscribe('order', (msg) => {
      if (msg.data) {
        setOrders((prev) => {
          const idx = prev.findIndex((o) => o.id === msg.data.id || o.client_order_id === msg.data.client_order_id);
          if (idx >= 0) {
            const next = [...prev];
            next[idx] = { ...next[idx], ...msg.data };
            return next;
          }
          return [msg.data, ...prev];
        });
      }
    });

    const unsubAlerts = wsService.subscribe('risk_alert', (msg) => {
      if (msg.data) {
        const newAlert: RiskAlertItem = {
          id: msg.data.id || String(Date.now()),
          timestamp: msg.timestamp || new Date().toISOString(),
          level: msg.data.severity || 'WARNING',
          reason: msg.data.message || msg.data.reason || 'Risk event detected',
          details: msg.data,
        };
        setAlerts((prev) => [newAlert, ...prev.slice(0, 49)]);
      }
    });

    // Periodic status refresh
    const interval = setInterval(() => {
      api.getSystemStatus().then(setStatus).catch(() => {});
    }, 5000);

    return () => {
      clearInterval(interval);
      unsubStatus();
      unsubPositions();
      unsubOrders();
      unsubAlerts();
      wsService.disconnect();
    };
  }, []);

  // System Controls
  const handlePause = async () => {
    try {
      await api.pauseTrading();
      const updated = await api.getSystemStatus();
      setStatus(updated);
    } catch (err: any) {
      alert(`Failed to pause: ${err.message}`);
    }
  };

  const handleResume = async () => {
    try {
      await api.resumeTrading();
      const updated = await api.getSystemStatus();
      setStatus(updated);
    } catch (err: any) {
      alert(`Failed to resume: ${err.message}`);
    }
  };

  const handleKillSwitch = async () => {
    try {
      await api.activateKillSwitch();
      await refreshAllData();
    } catch (err: any) {
      alert(`Failed to activate kill switch: ${err.message}`);
    }
  };

  const handleClosePosition = async (positionId: string) => {
    try {
      await api.closePosition(positionId);
      setPositions((prev) => prev.filter((p) => p.id !== positionId));
      const ords = await api.getOrders();
      setOrders(ords);
    } catch (err: any) {
      alert(`Failed to close position: ${err.message}`);
    }
  };

  const handleToggleStrategy = async (strategyId: string, currentActive: boolean) => {
    try {
      const updated = await api.updateStrategy(strategyId, { is_active: !currentActive });
      setStrategies((prev) => prev.map((s) => (s.id === strategyId ? { ...s, ...updated } : s)));
    } catch (err: any) {
      alert(`Failed to update strategy: ${err.message}`);
    }
  };

  const handleUpdateStrategyConfig = async (strategyId: string, updates: any) => {
    try {
      const updated = await api.updateStrategy(strategyId, updates);
      setStrategies((prev) => prev.map((s) => (s.id === strategyId ? { ...s, ...updated } : s)));
    } catch (err: any) {
      alert(`Failed to update strategy config: ${err.message}`);
    }
  };

  const handleRunBacktest = async (strategyId: string) => {
    try {
      setBacktestResult(null);
      const res = await api.runBacktest(strategyId, symbol, 30);
      setBacktestResult(res);
    } catch (err: any) {
      alert(`Backtest error: ${err.message}`);
    }
  };

  const handleAddConnection = async (payload: any) => {
    const conn = await api.createConnection(payload);
    setConnections((prev) => [conn, ...prev]);
  };

  const handleDeleteConnection = async (id: string) => {
    await api.deleteConnection(id);
    setConnections((prev) => prev.filter((c) => c.id !== id));
  };

  return (
    <div className="min-h-screen bg-darkbg text-gray-200 flex flex-col font-sans selection:bg-blue-600 selection:text-white">
      {/* Top Header */}
      <Header
        status={status}
        wsStatus={wsStatus}
        onPause={handlePause}
        onResume={handleResume}
        onKillSwitch={handleKillSwitch}
        onOpenConnections={() => setIsConnectionsOpen(true)}
        onOpenAnalytics={() => setIsAnalyticsOpen(true)}
      />

      {/* Main Terminal View */}
      <main className="flex-1 p-4 grid grid-cols-1 lg:grid-cols-12 gap-4 max-w-[1920px] mx-auto w-full">
        {/* Left Section (Market Chart, Positions, Orders) */}
        <div className="lg:col-span-8 flex flex-col space-y-4">
          <TradingViewChart
            symbol={symbol}
            timeframe={timeframe}
            onSymbolChange={setSymbol}
            onTimeframeChange={setTimeframe}
          />

          <PositionsTable
            positions={positions}
            onClosePosition={handleClosePosition}
          />

          <OrdersTable orders={orders} />
        </div>

        {/* Right Section (Strategies, Performance, Risk Alerts) */}
        <div className="lg:col-span-4 flex flex-col space-y-4">
          <StrategyPanel
            strategies={strategies}
            onToggleActive={handleToggleStrategy}
            onRunBacktest={handleRunBacktest}
            onUpdateStrategy={handleUpdateStrategyConfig}
            backtestResult={backtestResult}
          />

          <RiskAlertsFeed alerts={alerts} />
        </div>
      </main>

      {/* Connections Modal */}
      <ConnectionsModal
        isOpen={isConnectionsOpen}
        onClose={() => setIsConnectionsOpen(false)}
        connections={connections}
        onAddConnection={handleAddConnection}
        onDeleteConnection={handleDeleteConnection}
      />

      {/* Strategy Attribution & Completed Trades Analytics Modal */}
      <AnalyticsModal
        isOpen={isAnalyticsOpen}
        onClose={() => setIsAnalyticsOpen(false)}
      />
    </div>
  );
};

export default App;
