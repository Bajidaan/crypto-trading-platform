"""Exchange-Agnostic Domain Models, Exceptions, and IExchangeAdapter Interface."""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from app.core.decimal_utils import quantize_price, quantize_quantity, validate_order_filters
from app.models.enums import (
    ExchangeId,
    MarginMode,
    MarketType,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# =====================================================================
# 1. Exchange Adapter Exception Hierarchy
# =====================================================================


class ExchangeAdapterError(Exception):
    """Base exception for all exchange adapter errors."""

    def __init__(
        self,
        message: str,
        exchange_id: ExchangeId | None = None,
        error_code: str | int | None = None,
    ) -> None:
        super().__init__(message)
        self.exchange_id = exchange_id
        self.error_code = error_code


class ExchangeAuthenticationError(ExchangeAdapterError):
    """Raised when API key, secret, signature, or IP permission fails."""


class ExchangeRateLimitError(ExchangeAdapterError):
    """Raised on HTTP 429 / 418 or exchange-specific rate limit error codes."""

    def __init__(
        self,
        message: str,
        retry_after_seconds: float = 5.0,
        exchange_id: ExchangeId | None = None,
        error_code: str | int | None = None,
    ) -> None:
        super().__init__(message, exchange_id=exchange_id, error_code=error_code)
        self.retry_after_seconds = retry_after_seconds


class ExchangeInsufficientFundsError(ExchangeAdapterError):
    """Raised when account balance or margin is insufficient for an order."""


class ExchangeOrderRejectedError(ExchangeAdapterError):
    """Raised when the exchange matching engine rejects an order (filters, reduceOnly, etc.)."""


class ExchangeNetworkError(ExchangeAdapterError):
    """Raised on transient network timeouts, connection resets, or 5xx gateway errors."""


class OrderNotFoundError(ExchangeAdapterError):
    """Raised when querying or canceling an order that does not exist on the exchange."""


# =====================================================================
# 2. Normalized Exchange Domain Models (All Monetary Fields Use Decimal)
# =====================================================================


class MarketRules(BaseModel):
    """Exchange trading rules and precision filters for a specific symbol & market type."""

    model_config = ConfigDict(frozen=True)

    symbol: str  # Unified format e.g. "BTC/USDT"
    exchange_symbol: str  # Exchange-native symbol e.g. "BTCUSDT"
    market_type: MarketType
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal = Decimal("1000000000")
    min_notional: Decimal = Decimal("5.0")
    maker_fee_rate: Decimal = Decimal("0.001")
    taker_fee_rate: Decimal = Decimal("0.001")
    max_leverage: int = 10
    contract_size: Decimal = Decimal("1")

    def format_price(self, price: Decimal | str | int | float) -> Decimal:
        """Quantize price to the symbol's exact tick_size."""
        return quantize_price(price, self.tick_size)

    def format_quantity(self, quantity: Decimal | str | int | float) -> Decimal:
        """Quantize quantity DOWN to the symbol's exact step_size."""
        return quantize_quantity(quantity, self.step_size)

    def validate_order(self, price: Decimal, quantity: Decimal) -> tuple[bool, str | None]:
        """Validate quantized price and quantity against min_qty and min_notional."""
        return validate_order_filters(
            price=price,
            quantity=quantity,
            min_qty=self.min_qty,
            min_notional=self.min_notional,
        )


class AccountBalance(BaseModel):
    """Normalized wallet balance and margin snapshot."""

    model_config = ConfigDict(frozen=True)

    exchange_id: ExchangeId
    market_type: MarketType
    currency: str = "USDT"
    total_equity: Decimal
    available_balance: Decimal
    used_margin: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    timestamp: datetime = Field(default_factory=_utc_now)


class TickerSnapshot(BaseModel):
    """Normalized top-of-book and mark price snapshot."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    market_type: MarketType
    bid: Decimal
    ask: Decimal
    last_price: Decimal
    mark_price: Decimal | None = None
    volume_24h_usd: Decimal = Decimal("0")
    timestamp: datetime = Field(default_factory=_utc_now)


class CandleBar(BaseModel):
    """Normalized OHLCV candlestick bar."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    market_type: MarketType
    timeframe: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool = True


class NormalizedOrderRequest(BaseModel):
    """Normalized order placement request with mandatory clientOrderId."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    symbol: str
    market_type: MarketType
    side: OrderSide
    order_type: OrderType
    purpose: OrderPurpose = OrderPurpose.ENTRY
    quantity: Decimal
    price: Decimal | None = None
    stop_price: Decimal | None = None
    reduce_only: bool = False
    time_in_force: str = "GTC"
    working_type: str = "MARK_PRICE"
    trigger_direction: int | None = None  # 1 = rises to trigger, 2 = falls to trigger (Bybit)


class NormalizedOrderFill(BaseModel):
    """Normalized execution fill belonging to an order."""

    model_config = ConfigDict(frozen=True)

    exchange_trade_id: str
    fill_price: Decimal
    fill_qty: Decimal
    fee_amount: Decimal = Decimal("0")
    fee_currency: str = "USDT"
    executed_at: datetime = Field(default_factory=_utc_now)


class NormalizedOrderResponse(BaseModel):
    """Normalized exchange order state returned from placement or query."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    exchange_order_id: str
    symbol: str
    market_type: MarketType
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    requested_qty: Decimal
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    price: Decimal | None = None
    stop_price: Decimal | None = None
    fee_usd: Decimal = Decimal("0")
    reduce_only: bool = False
    fills: list[NormalizedOrderFill] = Field(default_factory=list)
    raw_response: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=_utc_now)


class ExchangePositionSnapshot(BaseModel):
    """Normalized open futures/margin position reported by the exchange."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    market_type: MarketType
    side: PositionSide
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal = Decimal("0")
    leverage: int = 1
    margin_mode: MarginMode = MarginMode.ISOLATED
    liquidation_price: Decimal | None = None


# =====================================================================
# 3. Abstract Exchange Adapter Interface (IExchangeAdapter)
# =====================================================================


class IExchangeAdapter(ABC):
    """Unified asynchronous interface for centralized cryptocurrency exchanges."""

    @property
    @abstractmethod
    def exchange_id(self) -> ExchangeId:
        """Return the exchange identifier (BINANCE or BYBIT)."""

    @property
    @abstractmethod
    def is_testnet(self) -> bool:
        """Return True if connected to sandbox/testnet endpoints."""

    @abstractmethod
    async def load_markets(
        self, market_type: MarketType, force_refresh: bool = False
    ) -> dict[str, MarketRules]:
        """Fetch and cache symbol precision/filter rules for the specified market type."""

    @abstractmethod
    async def get_market_rules(self, symbol: str, market_type: MarketType) -> MarketRules:
        """Return cached MarketRules for a symbol, loading markets first if needed."""

    @abstractmethod
    async def fetch_balance(self, market_type: MarketType) -> AccountBalance:
        """Fetch current USDT equity and available balance for Spot or USDT-M Futures."""

    @abstractmethod
    async def fetch_ticker(self, symbol: str, market_type: MarketType) -> TickerSnapshot:
        """Fetch current bid, ask, last price, and mark price for a symbol."""

    @abstractmethod
    async def fetch_ohlcv(
        self,
        symbol: str,
        market_type: MarketType,
        timeframe: str,
        since_ms: int | None = None,
        limit: int = 500,
    ) -> list[CandleBar]:
        """Fetch historical OHLCV bars for backtesting, warmup, or gap recovery."""

    @abstractmethod
    async def configure_futures_symbol(
        self,
        symbol: str,
        leverage: int = 3,
        margin_mode: MarginMode = MarginMode.ISOLATED,
        one_way_mode: bool = True,
    ) -> None:
        """Enforce One-Way Position Mode, Isolated Margin, and capped Leverage before trading."""

    @abstractmethod
    async def place_order(self, request: NormalizedOrderRequest) -> NormalizedOrderResponse:
        """Place a Spot or Futures order with filter quantization and idempotency."""

    @abstractmethod
    async def place_hard_stop_loss(
        self,
        symbol: str,
        market_type: MarketType,
        side: OrderSide,
        quantity: Decimal,
        stop_price: Decimal,
        client_order_id: str,
        limit_price: Decimal | None = None,
    ) -> NormalizedOrderResponse:
        """Place a Tier-1 exchange-native hard stop-loss order on the matching engine."""

    @abstractmethod
    async def amend_or_replace_stop_loss(
        self,
        existing_order_id: str,
        existing_client_order_id: str,
        symbol: str,
        market_type: MarketType,
        side: OrderSide,
        quantity: Decimal,
        new_stop_price: Decimal,
        new_client_order_id: str,
    ) -> NormalizedOrderResponse:
        """Atomically amend (or cancel-and-replace) an existing exchange-side hard stop-loss."""

    @abstractmethod
    async def cancel_order(
        self,
        exchange_order_id: str,
        symbol: str,
        market_type: MarketType,
        client_order_id: str | None = None,
    ) -> bool:
        """Cancel an open order by exchange_order_id or client_order_id."""

    @abstractmethod
    async def cancel_all_orders(self, symbol: str, market_type: MarketType) -> int:
        """Cancel all open orders for a symbol and return count canceled."""

    @abstractmethod
    async def fetch_order(
        self,
        symbol: str,
        market_type: MarketType,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> NormalizedOrderResponse:
        """Fetch order status by exchange_order_id or client_order_id."""

    @abstractmethod
    async def fetch_open_orders(
        self, symbol: str | None, market_type: MarketType
    ) -> list[NormalizedOrderResponse]:
        """Fetch all open orders for a symbol (or all symbols if None)."""

    @abstractmethod
    async def fetch_open_positions(
        self, market_type: MarketType, symbol: str | None = None
    ) -> list[ExchangePositionSnapshot]:
        """Fetch active open Futures positions from the exchange."""

    @abstractmethod
    async def close_position_market(
        self,
        symbol: str,
        market_type: MarketType,
        side: PositionSide,
        quantity: Decimal,
        client_order_id: str,
    ) -> NormalizedOrderResponse:
        """Close an open position immediately via a reduce-only market order."""

    @abstractmethod
    async def close(self) -> None:
        """Release underlying HTTP clients and resources."""
