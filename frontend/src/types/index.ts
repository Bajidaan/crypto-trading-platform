export interface SystemStatus {
  is_paused: boolean;
  kill_switch_active: boolean;
  max_concurrent_positions: number;
  max_daily_drawdown_pct: number;
  max_leverage_cap: number;
  open_paper_positions: number;
  total_paper_equity: number;
  active_stream_subscriptions: number;
  event_bus_published_count: number;
}

export interface PositionItem {
  id: string;
  symbol: string;
  market_type: string;
  execution_mode: 'PAPER' | 'LIVE';
  side: 'LONG' | 'SHORT';
  entry_price: number;
  current_price: number;
  current_qty: number;
  unrealized_pnl_usd: number;
  unrealized_pnl_pct: number;
  stop_loss_price: number | null;
  take_profit_price: number | null;
  leverage: number;
  margin_mode: string;
  status: string;
  strategy_name?: string;
  strategy_abbr?: string;
  realized_pnl_usd?: number;
  exit_reason?: string;
  opened_at: string;
}

export interface OrderItem {
  id: string;
  position_id: string;
  strategy_name?: string;
  strategy_abbr?: string;
  client_order_id: string;
  symbol: string;
  market_type: string;
  purpose: string;
  order_type: string;
  side: 'BUY' | 'SELL';
  requested_qty: number;
  filled_qty: number;
  limit_price: number | null;
  avg_fill_price: number | null;
  status: string;
  created_at: string;
}

export interface StrategyItem {
  id: string;
  name: string;
  strategy_class: string;
  execution_mode: 'PAPER' | 'LIVE';
  exchange_connection_id: string | null;
  market_type: string;
  symbols: string[];
  timeframe: string;
  parameters: Record<string, any>;
  risk_config: Record<string, any>;
  is_active: boolean;
  created_at: string;
}

export interface ConnectionItem {
  id: string;
  exchange_id: 'BINANCE' | 'BYBIT';
  label: string;
  api_key_masked: string;
  is_testnet: boolean;
  is_active: boolean;
  spot_capital_cap_usd: number | null;
  futures_capital_cap_usd: number | null;
  created_at: string;
}

export interface OHLCVCandle {
  time: number; // in unix seconds
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface RiskAlertItem {
  id: string;
  timestamp: string;
  level: 'INFO' | 'WARNING' | 'CRITICAL';
  reason: string;
  details?: Record<string, any>;
}

export interface WsMessage<T = any> {
  topic: string;
  type?: string;
  event?: string;
  data: T;
  timestamp: string;
}
