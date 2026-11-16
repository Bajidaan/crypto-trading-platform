// DEMO = Binance Demo Trading (demo funds); LIVE = the real Binance account.
export type TradingEnvironment = 'DEMO' | 'LIVE';

export interface SystemStatus {
  is_paused: boolean;
  kill_switch_active: boolean;
  max_concurrent_positions: number;
  max_daily_drawdown_pct: number;
  max_leverage_cap: number;
  environment: TradingEnvironment;
  connection_label: string | null;
  equity_usd: number | null;
  balances: Record<string, { total_equity: number; available_balance: number; unrealized_pnl: number }>;
  balance_error: string | null;
  balance_updated_at: string | null;
  open_positions: number;
  other_environment_open_positions: number;
  active_stream_subscriptions: number;
  event_bus_published_count: number;
}

export interface PositionItem {
  id: string;
  symbol: string;
  market_type: string;
  execution_mode: TradingEnvironment | 'PAPER';
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
  // Legacy field: strategies now trade on the app's active environment.
  execution_mode?: string;
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
  environment: TradingEnvironment;
  is_testnet: boolean;
  is_active: boolean;
  // The key the engine trades with on its account (the newest one).
  in_use: boolean;
  spot_capital_cap_usd: number | null;
  futures_capital_cap_usd: number | null;
  created_at: string;
  // Only returned when a key is added: balances Binance reported, and caveats.
  balances?: Record<string, number>;
  warnings?: string[];
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
