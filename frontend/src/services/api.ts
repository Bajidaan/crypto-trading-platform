import type {
  ConnectionItem,
  OHLCVCandle,
  OrderItem,
  PositionItem,
  StrategyItem,
  SystemStatus,
} from '../types';

const getApiBase = (): string => {
  if (typeof window === 'undefined') return 'http://localhost:8000/api/v1';
  const { protocol, hostname, port } = window.location;
  if (port === '5173') {
    return `${protocol}//${hostname}:8000/api/v1`;
  }
  return `${protocol}//${window.location.host}/api/v1`;
};

const API_BASE = getApiBase();

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let errorDetail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      errorDetail = body.detail || body.message || errorDetail;
    } catch {
      // ignore
    }
    throw new Error(errorDetail);
  }
  return res.json();
}

export const api = {
  // System
  async getSystemStatus(): Promise<SystemStatus> {
    const res = await fetch(`${API_BASE}/system/status`);
    return handleResponse<SystemStatus>(res);
  },

  async pauseTrading(): Promise<{ status: string }> {
    const res = await fetch(`${API_BASE}/system/pause`, { method: 'POST' });
    return handleResponse(res);
  },

  async resumeTrading(): Promise<{ status: string }> {
    const res = await fetch(`${API_BASE}/system/resume`, { method: 'POST' });
    return handleResponse(res);
  },

  async activateKillSwitch(): Promise<{ status: string }> {
    const res = await fetch(`${API_BASE}/system/kill-switch`, { method: 'POST' });
    return handleResponse(res);
  },

  // Positions
  async getPositions(): Promise<PositionItem[]> {
    const res = await fetch(`${API_BASE}/positions`);
    return handleResponse<PositionItem[]>(res);
  },

  async closePosition(positionId: string): Promise<{ status: string; id: string }> {
    const res = await fetch(`${API_BASE}/positions/${positionId}/close`, { method: 'POST' });
    return handleResponse(res);
  },

  async getPositionsAnalytics(): Promise<any> {
    const res = await fetch(`${API_BASE}/positions/analytics`);
    return handleResponse<any>(res);
  },

  // Orders
  async getOrders(): Promise<OrderItem[]> {
    const res = await fetch(`${API_BASE}/orders`);
    return handleResponse<OrderItem[]>(res);
  },

  // Strategies
  async getStrategies(): Promise<StrategyItem[]> {
    const res = await fetch(`${API_BASE}/strategies`);
    return handleResponse<StrategyItem[]>(res);
  },

  async updateStrategy(
    strategyId: string,
    updates: {
      is_active?: boolean;
      timeframe?: string;
      symbols?: string[];
      execution_mode?: 'PAPER' | 'LIVE';
      parameters?: Record<string, any>;
    }
  ): Promise<StrategyItem> {
    const res = await fetch(`${API_BASE}/strategies/${strategyId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates),
    });
    return handleResponse<StrategyItem>(res);
  },

  async runBacktest(
    strategyId: string,
    symbol: string = 'BTC/USDT',
    lookbackDays: number = 30
  ): Promise<any> {
    const res = await fetch(`${API_BASE}/strategies/${strategyId}/backtest`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, lookback_days: lookbackDays }),
    });
    return handleResponse(res);
  },

  // Exchange Connections
  async getConnections(): Promise<ConnectionItem[]> {
    const res = await fetch(`${API_BASE}/connections`);
    return handleResponse<ConnectionItem[]>(res);
  },

  async createConnection(payload: {
    exchange_id: string;
    label: string;
    api_key: string;
    api_secret: string;
    is_testnet: boolean;
    spot_capital_cap_usd?: number;
    futures_capital_cap_usd?: number;
  }): Promise<ConnectionItem> {
    const res = await fetch(`${API_BASE}/connections`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return handleResponse<ConnectionItem>(res);
  },

  async deleteConnection(connectionId: string): Promise<{ status: string; id: string }> {
    const res = await fetch(`${API_BASE}/connections/${connectionId}`, { method: 'DELETE' });
    return handleResponse(res);
  },

  // Market Data
  async getCandles(
    symbol: string,
    timeframe: string = '15m',
    limit: number = 100
  ): Promise<OHLCVCandle[]> {
    try {
      const res = await fetch(
        `${API_BASE}/market/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=${limit}`
      );
      const data = await handleResponse<any[]>(res);
      if (Array.isArray(data) && data.length > 0) {
        return data.map((c) => ({
          time: c.time != null
            ? (typeof c.time === 'number' ? c.time : Number(c.time))
            : (c.timestamp ? Math.floor(new Date(c.timestamp).getTime() / 1000) : Math.floor(Date.now() / 1000)),
          open: Number(c.open),
          high: Number(c.high),
          low: Number(c.low),
          close: Number(c.close),
          volume: Number(c.volume),
        }));
      }
    } catch (err) {
      console.warn('[API] Could not fetch market candles from backend, generating local fallback bars:', err);
    }

    // Fallback simulated candles
    const now = Math.floor(Date.now() / 1000);
    const base = symbol.includes('BTC') ? 65200 : symbol.includes('ETH') ? 3450 : symbol.includes('SOL') ? 165 : 590;
    return Array.from({ length: 80 }, (_, i) => {
      const t = now - (80 - i) * 900;
      const variation = Math.sin(i / 6) * (base * 0.02) + (i * (base * 0.0005));
      const o = Number((base + variation).toFixed(2));
      const c = Number((o + (Math.sin(i * 1.5) * (base * 0.008))).toFixed(2));
      const h = Number((Math.max(o, c) + (base * 0.004)).toFixed(2));
      const l = Number((Math.min(o, c) - (base * 0.004)).toFixed(2));
      return {
        time: t,
        open: o,
        high: h,
        low: l,
        close: c,
        volume: Number((100 + Math.abs(Math.sin(i)) * 250).toFixed(2)),
      };
    });
  },

  async getTicker(symbol: string): Promise<any> {
    const res = await fetch(`${API_BASE}/market/ticker?symbol=${encodeURIComponent(symbol)}`);
    return handleResponse(res);
  },
};
