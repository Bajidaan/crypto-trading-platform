"""Bybit v5 Unified Trading Account (Spot & USDT Linear Perpetual) Exchange Adapter."""

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode
import httpx

from app.core.decimal_utils import CLIENT_ORDER_ID_PATTERN, to_decimal
from app.domains.cex.adapters.base import (
    AccountBalance,
    CandleBar,
    ExchangeAdapterError,
    ExchangeAuthenticationError,
    ExchangeInsufficientFundsError,
    ExchangeNetworkError,
    ExchangeOrderRejectedError,
    ExchangePositionSnapshot,
    ExchangeRateLimitError,
    IExchangeAdapter,
    MarketRules,
    NormalizedOrderRequest,
    NormalizedOrderResponse,
    OrderNotFoundError,
    TickerSnapshot,
)
from app.domains.cex.adapters.rate_limiter import (
    AsyncTokenBucketRateLimiter,
    execute_with_retry,
    submit_order_idempotently,
)
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

BYBIT_INTERVAL_MAP = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",
    "1d": "D",
    "1w": "W",
}


def _to_bybit_symbol(symbol: str) -> str:
    return symbol.replace("/", "").replace(":USDT", "").upper().strip()


def _to_bybit_category(market_type: MarketType) -> str:
    return "spot" if market_type == MarketType.SPOT else "linear"


def _map_bybit_order_status(raw_status: str) -> OrderStatus:
    status_map = {
        "CREATED": OrderStatus.OPEN,
        "NEW": OrderStatus.OPEN,
        "UNTRIGGERED": OrderStatus.OPEN,
        "TRIGGERED": OrderStatus.OPEN,
        "PARTIALLYFILLED": OrderStatus.PARTIALLY_FILLED,
        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
        "FILLED": OrderStatus.FILLED,
        "CANCELLED": OrderStatus.CANCELED,
        "CANCELED": OrderStatus.CANCELED,
        "PARTIALLYFILLEDCANCELED": OrderStatus.CANCELED,
        "DEACTIVATED": OrderStatus.CANCELED,
        "REJECTED": OrderStatus.REJECTED,
    }
    return status_map.get(raw_status.upper(), OrderStatus.OPEN)


class BybitAdapter(IExchangeAdapter):
    """Asynchronous Bybit v5 API adapter for Spot and USDT Linear Perpetual Futures."""

    LIVE_URL = "https://api.bybit.com"
    TESTNET_URL = "https://api-testnet.bybit.com"
    RECV_WINDOW = "5000"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        is_testnet: bool = True,
        max_leverage_cap: int = 10,
        rate_limiter: AsyncTokenBucketRateLimiter | None = None,
        http_client: httpx.AsyncClient | None = None,
        time_ms_fn: Callable[[], int] | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._api_secret = api_secret.strip()
        self._is_testnet = is_testnet
        self._max_leverage_cap = max_leverage_cap
        self._rate_limiter = rate_limiter or AsyncTokenBucketRateLimiter(
            capacity=30.0, refill_rate_per_sec=15.0
        )
        self._external_client = http_client is not None
        self._client = http_client or httpx.AsyncClient(timeout=15.0)
        self._time_ms_fn = time_ms_fn or (lambda: int(time.time() * 1000))
        self._markets_cache: dict[MarketType, dict[str, MarketRules]] = {
            MarketType.SPOT: {},
            MarketType.FUTURES_USDT: {},
        }

    @property
    def exchange_id(self) -> ExchangeId:
        return ExchangeId.BYBIT

    @property
    def is_testnet(self) -> bool:
        return self._is_testnet

    @property
    def _base_url(self) -> str:
        return self.TESTNET_URL if self._is_testnet else self.LIVE_URL

    def _compute_signature(self, timestamp_ms: int, payload_str: str) -> str:
        prehash = f"{timestamp_ms}{self._api_key}{self.RECV_WINDOW}{payload_str}"
        return hmac.new(
            self._api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _raise_mapped_error(self, status_code: int, ret_code: int | None, ret_msg: str) -> None:
        if status_code in (429, 418) or ret_code in (10006, 10018):
            raise ExchangeRateLimitError(
                f"Bybit rate limit exceeded ({ret_code}): {ret_msg}",
                retry_after_seconds=5.0,
                exchange_id=ExchangeId.BYBIT,
                error_code=ret_code or status_code,
            )
        if status_code in (401, 403) or ret_code in (10002, 10003, 10004, 10005, 33004):
            raise ExchangeAuthenticationError(
                f"Bybit authentication error ({ret_code}): {ret_msg}",
                exchange_id=ExchangeId.BYBIT,
                error_code=ret_code or status_code,
            )
        if ret_code in (110007, 110012, 110045, 170131):
            raise ExchangeInsufficientFundsError(
                f"Bybit insufficient balance/margin ({ret_code}): {ret_msg}",
                exchange_id=ExchangeId.BYBIT,
                error_code=ret_code,
            )
        if ret_code in (110001, 170213):
            raise OrderNotFoundError(
                f"Bybit order not found ({ret_code}): {ret_msg}",
                exchange_id=ExchangeId.BYBIT,
                error_code=ret_code,
            )
        if ret_code in (10001, 110003, 110017, 110092, 110093, 170134, 170137, 170140):
            raise ExchangeOrderRejectedError(
                f"Bybit order rejected ({ret_code}): {ret_msg}",
                exchange_id=ExchangeId.BYBIT,
                error_code=ret_code,
            )
        if status_code >= 500 or ret_code in (10000, 10016):
            raise ExchangeNetworkError(
                f"Bybit server/timeout error (HTTP {status_code}, retCode={ret_code}): {ret_msg}",
                exchange_id=ExchangeId.BYBIT,
                error_code=ret_code or status_code,
            )
        raise ExchangeAdapterError(
            f"Bybit API error (HTTP {status_code}, retCode={ret_code}): {ret_msg}",
            exchange_id=ExchangeId.BYBIT,
            error_code=ret_code or status_code,
        )

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> dict[str, Any]:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        method_upper = method.upper()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        content: bytes | None = None

        if method_upper == "GET":
            query_str = urlencode(clean_params, doseq=True)
            url = f"{self._base_url}{path}" + (f"?{query_str}" if query_str else "")
            payload_str = query_str
        else:
            url = f"{self._base_url}{path}"
            payload_str = json.dumps(clean_params, separators=(",", ":"))
            content = payload_str.encode("utf-8")

        if signed:
            ts = self._time_ms_fn()
            sig = self._compute_signature(ts, payload_str)
            headers.update(
                {
                    "X-BAPI-API-KEY": self._api_key,
                    "X-BAPI-TIMESTAMP": str(ts),
                    "X-BAPI-RECV-WINDOW": self.RECV_WINDOW,
                    "X-BAPI-SIGN": sig,
                }
            )

        try:
            response = await self._client.request(
                method=method_upper,
                url=url,
                headers=headers,
                content=content,
            )
        except httpx.RequestError as exc:
            raise ExchangeNetworkError(
                f"Bybit network request failed ({method_upper} {path}): {exc}",
                exchange_id=ExchangeId.BYBIT,
            ) from exc

        try:
            data = response.json()
        except Exception as exc:
            if response.status_code >= 500:
                raise ExchangeNetworkError(
                    f"Bybit 5xx error ({response.status_code})",
                    exchange_id=ExchangeId.BYBIT,
                    error_code=response.status_code,
                ) from exc
            raise ExchangeAdapterError(
                f"Bybit invalid JSON response (HTTP {response.status_code})",
                exchange_id=ExchangeId.BYBIT,
                error_code=response.status_code,
            ) from exc

        ret_code = int(data.get("retCode", 0)) if isinstance(data, dict) else 0
        ret_msg = str(data.get("retMsg", "OK")) if isinstance(data, dict) else str(data)

        if response.status_code >= 400 or ret_code != 0:
            self._raise_mapped_error(response.status_code, ret_code, ret_msg)

        return data.get("result", {}) if isinstance(data, dict) else {}

    async def load_markets(
        self, market_type: MarketType, force_refresh: bool = False
    ) -> dict[str, MarketRules]:
        if self._markets_cache[market_type] and not force_refresh:
            return self._markets_cache[market_type]

        category = _to_bybit_category(market_type)
        result = await execute_with_retry(
            lambda: self._request(
                "GET",
                "/v5/market/instruments-info",
                params={"category": category, "limit": 1000},
                signed=False,
            ),
            rate_limiter=self._rate_limiter,
            weight=2.0,
        )

        rules_map: dict[str, MarketRules] = {}
        for item in result.get("list", []):
            status = item.get("status", "Trading")
            if status != "Trading":
                continue
            base_coin = item.get("baseCoin", "")
            quote_coin = item.get("quoteCoin", "")
            if not base_coin or not quote_coin:
                continue

            unified_symbol = f"{base_coin.upper()}/{quote_coin.upper()}"
            exch_symbol = item.get("symbol", f"{base_coin}{quote_coin}")

            pf = item.get("priceFilter", {})
            lf = item.get("lotSizeFilter", {})
            lev_f = item.get("leverageFilter", {})

            tick_size = to_decimal(pf.get("tickSize", "0.01"))
            step_size = to_decimal(
                lf.get("qtyStep") or lf.get("basePrecision") or "0.001"
            )
            min_qty = to_decimal(lf.get("minOrderQty", "0.001"))
            max_qty = to_decimal(lf.get("maxOrderQty", "1000000"))
            min_notional = to_decimal(
                lf.get("minNotionalValue") or lf.get("minOrderAmt") or "5.0"
            )
            max_lev_raw = int(float(lev_f.get("maxLeverage", self._max_leverage_cap)))

            rules_map[unified_symbol] = MarketRules(
                symbol=unified_symbol,
                exchange_symbol=exch_symbol,
                market_type=market_type,
                base_asset=base_coin,
                quote_asset=quote_coin,
                tick_size=tick_size,
                step_size=step_size,
                min_qty=min_qty,
                max_qty=max_qty,
                min_notional=min_notional,
                maker_fee_rate=Decimal("0.001") if market_type == MarketType.SPOT else Decimal("0.0002"),
                taker_fee_rate=Decimal("0.001") if market_type == MarketType.SPOT else Decimal("0.00055"),
                max_leverage=min(max_lev_raw, self._max_leverage_cap)
                if market_type == MarketType.FUTURES_USDT
                else 1,
            )

        self._markets_cache[market_type] = rules_map
        return rules_map

    async def get_market_rules(self, symbol: str, market_type: MarketType) -> MarketRules:
        markets = await self.load_markets(market_type)
        if symbol not in markets:
            raise ExchangeAdapterError(
                f"Symbol {symbol} not found in Bybit {market_type.value} markets",
                exchange_id=ExchangeId.BYBIT,
            )
        return markets[symbol]

    async def fetch_balance(self, market_type: MarketType) -> AccountBalance:
        result = await execute_with_retry(
            lambda: self._request(
                "GET",
                "/v5/account/wallet-balance",
                params={"accountType": "UNIFIED", "coin": "USDT"},
                signed=True,
            ),
            rate_limiter=self._rate_limiter,
            weight=1.0,
        )
        accounts = result.get("list", [])
        if not accounts:
            return AccountBalance(
                exchange_id=ExchangeId.BYBIT,
                market_type=market_type,
                currency="USDT",
                total_equity=Decimal("0"),
                available_balance=Decimal("0"),
            )

        acct = accounts[0]
        for coin_row in acct.get("coin", []):
            if coin_row.get("coin") == "USDT":
                equity = to_decimal(coin_row.get("equity") or acct.get("totalEquity") or "0")
                wallet_bal = to_decimal(coin_row.get("walletBalance") or equity)
                used_margin = to_decimal(
                    coin_row.get("totalPositionIM") or acct.get("totalInitialMargin") or "0"
                )
                locked = to_decimal(coin_row.get("locked") or "0")
                avail_raw = coin_row.get("availableToWithdraw") or acct.get("totalAvailableBalance")
                avail = (
                    to_decimal(avail_raw)
                    if avail_raw not in (None, "")
                    else max(Decimal("0"), wallet_bal - used_margin - locked)
                )
                upnl = to_decimal(coin_row.get("unrealisedPnl") or acct.get("totalPerpUPL") or "0")
                return AccountBalance(
                    exchange_id=ExchangeId.BYBIT,
                    market_type=market_type,
                    currency="USDT",
                    total_equity=equity,
                    available_balance=avail,
                    used_margin=used_margin,
                    unrealized_pnl=upnl,
                )

        return AccountBalance(
            exchange_id=ExchangeId.BYBIT,
            market_type=market_type,
            currency="USDT",
            total_equity=to_decimal(acct.get("totalEquity") or "0"),
            available_balance=to_decimal(acct.get("totalAvailableBalance") or "0"),
            used_margin=to_decimal(acct.get("totalInitialMargin") or "0"),
            unrealized_pnl=to_decimal(acct.get("totalPerpUPL") or "0"),
        )

    async def fetch_ticker(self, symbol: str, market_type: MarketType) -> TickerSnapshot:
        category = _to_bybit_category(market_type)
        exch_symbol = _to_bybit_symbol(symbol)
        result = await execute_with_retry(
            lambda: self._request(
                "GET",
                "/v5/market/tickers",
                params={"category": category, "symbol": exch_symbol},
                signed=False,
            ),
            rate_limiter=self._rate_limiter,
            weight=1.0,
        )
        items = result.get("list", [])
        if not items:
            raise ExchangeAdapterError(
                f"Ticker not found for {symbol} on Bybit",
                exchange_id=ExchangeId.BYBIT,
            )
        row = items[0]
        last = to_decimal(row.get("lastPrice", "0"))
        bid = to_decimal(row.get("bid1Price") or last)
        ask = to_decimal(row.get("ask1Price") or last)
        mark = to_decimal(row.get("markPrice") or last)
        vol = to_decimal(row.get("turnover24h", "0"))
        return TickerSnapshot(
            symbol=symbol,
            market_type=market_type,
            bid=bid,
            ask=ask,
            last_price=last,
            mark_price=mark,
            volume_24h_usd=vol,
        )

    async def fetch_ohlcv(
        self,
        symbol: str,
        market_type: MarketType,
        timeframe: str,
        since_ms: int | None = None,
        limit: int = 500,
    ) -> list[CandleBar]:
        category = _to_bybit_category(market_type)
        exch_symbol = _to_bybit_symbol(symbol)
        bybit_interval = BYBIT_INTERVAL_MAP.get(timeframe, timeframe)
        params: dict[str, Any] = {
            "category": category,
            "symbol": exch_symbol,
            "interval": bybit_interval,
            "limit": min(limit, 1000),
        }
        if since_ms is not None:
            params["start"] = since_ms

        result = await execute_with_retry(
            lambda: self._request("GET", "/v5/market/kline", params=params, signed=False),
            rate_limiter=self._rate_limiter,
            weight=1.0,
        )
        rows = result.get("list", [])
        # Bybit returns klines in descending order (newest first); sort ascending by open_time
        sorted_rows = sorted(rows, key=lambda r: int(r[0]))
        bars: list[CandleBar] = []
        for idx, row in enumerate(sorted_rows):
            open_ms = int(row[0])
            bars.append(
                CandleBar(
                    symbol=symbol,
                    market_type=market_type,
                    timeframe=timeframe,
                    open_time=datetime.fromtimestamp(open_ms / 1000.0, tz=timezone.utc),
                    open=to_decimal(row[1]),
                    high=to_decimal(row[2]),
                    low=to_decimal(row[3]),
                    close=to_decimal(row[4]),
                    volume=to_decimal(row[5]),
                    is_closed=True if idx < len(sorted_rows) - 1 else True,
                )
            )
        return bars

    async def configure_futures_symbol(
        self,
        symbol: str,
        leverage: int = 3,
        margin_mode: MarginMode = MarginMode.ISOLATED,
        one_way_mode: bool = True,
    ) -> None:
        exch_symbol = _to_bybit_symbol(symbol)
        capped_lev = str(max(1, min(int(leverage), self._max_leverage_cap)))

        # 1. Switch Position Mode to One-Way (mode=0)
        try:
            await execute_with_retry(
                lambda: self._request(
                    "POST",
                    "/v5/position/switch-mode",
                    params={
                        "category": "linear",
                        "symbol": exch_symbol,
                        "mode": 0 if one_way_mode else 3,
                    },
                    signed=True,
                ),
                rate_limiter=self._rate_limiter,
            )
        except ExchangeAdapterError as exc:
            # 110025 / 34036: Position mode is not modified
            if exc.error_code not in (110025, 34036):
                raise

        # 2. Switch Isolated/Cross Margin Mode (tradeMode: 1 = Isolated, 0 = Cross)
        try:
            await execute_with_retry(
                lambda: self._request(
                    "POST",
                    "/v5/position/switch-isolated",
                    params={
                        "category": "linear",
                        "symbol": exch_symbol,
                        "tradeMode": 1 if margin_mode == MarginMode.ISOLATED else 0,
                        "buyLeverage": capped_lev,
                        "sellLeverage": capped_lev,
                    },
                    signed=True,
                ),
                rate_limiter=self._rate_limiter,
            )
        except ExchangeAdapterError as exc:
            # 110026 / 110043 / 100028 (UTA accounts manage isolated mode or already unmodified)
            if exc.error_code not in (110026, 110043, 100028):
                raise

        # 3. Set Leverage
        try:
            await execute_with_retry(
                lambda: self._request(
                    "POST",
                    "/v5/position/set-leverage",
                    params={
                        "category": "linear",
                        "symbol": exch_symbol,
                        "buyLeverage": capped_lev,
                        "sellLeverage": capped_lev,
                    },
                    signed=True,
                ),
                rate_limiter=self._rate_limiter,
            )
        except ExchangeAdapterError as exc:
            # 110043: Set leverage not modified
            if exc.error_code != 110043:
                raise

    def _parse_order_item(
        self,
        item: dict[str, Any],
        symbol: str,
        market_type: MarketType,
        fallback_req: NormalizedOrderRequest | None = None,
    ) -> NormalizedOrderResponse:
        client_oid = str(
            item.get("orderLinkId")
            or (fallback_req.client_order_id if fallback_req else "")
        )
        exch_oid = str(item.get("orderId", ""))
        raw_side = str(
            item.get("side") or (fallback_req.side.value if fallback_req else "Buy")
        ).upper()
        side = OrderSide.BUY if raw_side == "BUY" else OrderSide.SELL

        raw_type = str(item.get("orderType", "Market")).upper()
        trigger_price_raw = item.get("triggerPrice")
        has_trigger = trigger_price_raw not in (None, "", "0", "0.0", "0.00")

        if has_trigger:
            order_type = OrderType.STOP_MARKET if raw_type == "MARKET" else OrderType.STOP_LIMIT
        elif fallback_req is not None and "orderType" not in item:
            order_type = fallback_req.order_type
        else:
            order_type = OrderType.MARKET if raw_type == "MARKET" else OrderType.LIMIT

        raw_status = str(item.get("orderStatus", "New"))
        status = _map_bybit_order_status(raw_status)

        req_qty = to_decimal(
            item.get("qty") or (fallback_req.quantity if fallback_req else "0")
        )
        filled_qty = to_decimal(item.get("cumExecQty", "0"))
        avg_price_raw = to_decimal(item.get("avgPrice", "0"))
        price_raw = to_decimal(
            item.get("price") or (fallback_req.price if fallback_req and fallback_req.price else "0")
        )
        stop_raw = to_decimal(
            trigger_price_raw
            if has_trigger
            else (fallback_req.stop_price if fallback_req and fallback_req.stop_price else "0")
        )
        fee_usd = to_decimal(item.get("cumExecFee", "0"))

        return NormalizedOrderResponse(
            client_order_id=client_oid,
            exchange_order_id=exch_oid,
            symbol=symbol,
            market_type=market_type,
            side=side,
            order_type=order_type,
            status=status,
            requested_qty=req_qty,
            filled_qty=filled_qty,
            avg_fill_price=avg_price_raw if avg_price_raw > Decimal("0") else None,
            price=price_raw if price_raw > Decimal("0") else None,
            stop_price=stop_raw if stop_raw > Decimal("0") else None,
            fee_usd=fee_usd,
            reduce_only=bool(
                item.get("reduceOnly")
                if "reduceOnly" in item
                else (fallback_req.reduce_only if fallback_req else False)
            ),
            raw_response=item,
        )

    async def place_order(self, request: NormalizedOrderRequest) -> NormalizedOrderResponse:
        if not CLIENT_ORDER_ID_PATTERN.match(request.client_order_id):
            raise ExchangeOrderRejectedError(
                f"Invalid client_order_id '{request.client_order_id}'",
                exchange_id=ExchangeId.BYBIT,
            )

        rules = await self.get_market_rules(request.symbol, request.market_type)
        q_qty = rules.format_quantity(request.quantity)
        q_price = rules.format_price(request.price) if request.price is not None else None
        q_stop = rules.format_price(request.stop_price) if request.stop_price is not None else None

        if q_qty < rules.min_qty:
            raise ExchangeOrderRejectedError(
                f"Quantized quantity {q_qty} is below minimum {rules.min_qty} for {request.symbol}",
                exchange_id=ExchangeId.BYBIT,
            )
        ref_price = q_price or q_stop
        if ref_price is not None and not request.reduce_only:
            ok, err = rules.validate_order(price=ref_price, quantity=q_qty)
            if not ok:
                raise ExchangeOrderRejectedError(
                    err or "Order failed filter validation",
                    exchange_id=ExchangeId.BYBIT,
                )

        category = _to_bybit_category(request.market_type)
        params: dict[str, Any] = {
            "category": category,
            "symbol": rules.exchange_symbol,
            "side": "Buy" if request.side == OrderSide.BUY else "Sell",
            "qty": str(q_qty),
            "orderLinkId": request.client_order_id,
        }

        if request.market_type == MarketType.SPOT and request.order_type == OrderType.MARKET:
            # Bybit v5 Spot Market BUY defaults to quote currency unless marketUnit="baseCoin" is specified
            params["marketUnit"] = "baseCoin"

        if request.order_type in (OrderType.MARKET, OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET):
            params["orderType"] = "Market"
        else:
            params["orderType"] = "Limit"
            if q_price is None:
                raise ExchangeOrderRejectedError("Limit order requires price", exchange_id=ExchangeId.BYBIT)
            params["price"] = str(q_price)
            params["timeInForce"] = request.time_in_force

        if request.order_type in (
            OrderType.STOP_MARKET,
            OrderType.STOP_LIMIT,
            OrderType.TAKE_PROFIT_MARKET,
            OrderType.TAKE_PROFIT_LIMIT,
        ):
            if q_stop is None:
                raise ExchangeOrderRejectedError(
                    f"{request.order_type.value} requires stop_price",
                    exchange_id=ExchangeId.BYBIT,
                )
            params["triggerPrice"] = str(q_stop)
            if request.market_type == MarketType.FUTURES_USDT:
                params["triggerBy"] = "MarkPrice" if request.working_type == "MARK_PRICE" else "LastPrice"
                # triggerDirection: 1 = rises to trigger, 2 = falls to trigger
                if request.trigger_direction is not None:
                    params["triggerDirection"] = request.trigger_direction
                elif request.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT):
                    params["triggerDirection"] = 2 if request.side == OrderSide.SELL else 1
                else:
                    params["triggerDirection"] = 1 if request.side == OrderSide.SELL else 2
            else:
                params["orderFilter"] = "tpslOrder"

        if request.reduce_only and request.market_type == MarketType.FUTURES_USDT:
            params["reduceOnly"] = True
            params["closeOnTrigger"] = True

        quantized_req = request.model_copy(
            update={"quantity": q_qty, "price": q_price, "stop_price": q_stop}
        )

        async def _submit() -> NormalizedOrderResponse:
            res = await self._request("POST", "/v5/order/create", params=params, signed=True)
            return self._parse_order_item(
                res, request.symbol, request.market_type, fallback_req=quantized_req
            )

        async def _query_existing() -> NormalizedOrderResponse:
            return await self.fetch_order(
                symbol=request.symbol,
                market_type=request.market_type,
                client_order_id=request.client_order_id,
            )

        return await submit_order_idempotently(
            submit_fn=_submit,
            query_existing_fn=_query_existing,
            rate_limiter=self._rate_limiter,
            weight=1.0,
        )

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
        if market_type == MarketType.FUTURES_USDT:
            # SELL stop protects LONG (triggers when price falls -> triggerDirection=2)
            # BUY stop protects SHORT (triggers when price rises -> triggerDirection=1)
            trig_dir = 2 if side == OrderSide.SELL else 1
            req = NormalizedOrderRequest(
                client_order_id=client_order_id,
                symbol=symbol,
                market_type=MarketType.FUTURES_USDT,
                side=side,
                order_type=OrderType.STOP_MARKET,
                purpose=OrderPurpose.HARD_STOP_LOSS,
                quantity=quantity,
                stop_price=stop_price,
                reduce_only=True,
                working_type="MARK_PRICE",
                trigger_direction=trig_dir,
            )
            return await self.place_order(req)

        if limit_price is None:
            buffer = Decimal("0.997") if side == OrderSide.SELL else Decimal("1.003")
            limit_price = stop_price * buffer

        req = NormalizedOrderRequest(
            client_order_id=client_order_id,
            symbol=symbol,
            market_type=MarketType.SPOT,
            side=side,
            order_type=OrderType.STOP_LIMIT,
            purpose=OrderPurpose.HARD_STOP_LOSS,
            quantity=quantity,
            price=limit_price,
            stop_price=stop_price,
            reduce_only=False,
            time_in_force="GTC",
        )
        return await self.place_order(req)

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
        # On Bybit Linear Futures, use native atomic /v5/order/amend first so the position
        # is never left unprotected on the orderbook during the update.
        if market_type == MarketType.FUTURES_USDT:
            rules = await self.get_market_rules(symbol, market_type)
            q_qty = rules.format_quantity(quantity)
            q_stop = rules.format_price(new_stop_price)
            amend_params: dict[str, Any] = {
                "category": "linear",
                "symbol": rules.exchange_symbol,
                "qty": str(q_qty),
                "triggerPrice": str(q_stop),
            }
            if existing_order_id:
                amend_params["orderId"] = existing_order_id
            else:
                amend_params["orderLinkId"] = existing_client_order_id

            try:
                res = await execute_with_retry(
                    lambda: self._request("POST", "/v5/order/amend", params=amend_params, signed=True),
                    rate_limiter=self._rate_limiter,
                    weight=1.0,
                )
                return NormalizedOrderResponse(
                    client_order_id=str(res.get("orderLinkId") or existing_client_order_id),
                    exchange_order_id=str(res.get("orderId") or existing_order_id),
                    symbol=symbol,
                    market_type=MarketType.FUTURES_USDT,
                    side=side,
                    order_type=OrderType.STOP_MARKET,
                    status=OrderStatus.OPEN,
                    requested_qty=q_qty,
                    stop_price=q_stop,
                    reduce_only=True,
                    raw_response=res,
                )
            except ExchangeAdapterError:
                # Fallback to cancel-and-replace if amend is rejected
                pass

        try:
            await self.cancel_order(
                exchange_order_id=existing_order_id,
                symbol=symbol,
                market_type=market_type,
                client_order_id=existing_client_order_id,
            )
        except OrderNotFoundError:
            pass

        return await self.place_hard_stop_loss(
            symbol=symbol,
            market_type=market_type,
            side=side,
            quantity=quantity,
            stop_price=new_stop_price,
            client_order_id=new_client_order_id,
        )

    async def cancel_order(
        self,
        exchange_order_id: str,
        symbol: str,
        market_type: MarketType,
        client_order_id: str | None = None,
    ) -> bool:
        category = _to_bybit_category(market_type)
        exch_symbol = _to_bybit_symbol(symbol)
        params: dict[str, Any] = {
            "category": category,
            "symbol": exch_symbol,
        }
        if exchange_order_id:
            params["orderId"] = exchange_order_id
        elif client_order_id:
            params["orderLinkId"] = client_order_id
        else:
            raise ValueError("Either exchange_order_id or client_order_id must be provided")

        await execute_with_retry(
            lambda: self._request("POST", "/v5/order/cancel", params=params, signed=True),
            rate_limiter=self._rate_limiter,
            weight=1.0,
        )
        return True

    async def cancel_all_orders(self, symbol: str, market_type: MarketType) -> int:
        category = _to_bybit_category(market_type)
        exch_symbol = _to_bybit_symbol(symbol)
        res = await execute_with_retry(
            lambda: self._request(
                "POST",
                "/v5/order/cancel-all",
                params={"category": category, "symbol": exch_symbol},
                signed=True,
            ),
            rate_limiter=self._rate_limiter,
            weight=1.0,
        )
        canceled_list = res.get("list", [])
        return len(canceled_list)

    async def fetch_order(
        self,
        symbol: str,
        market_type: MarketType,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> NormalizedOrderResponse:
        category = _to_bybit_category(market_type)
        exch_symbol = _to_bybit_symbol(symbol)
        params: dict[str, Any] = {
            "category": category,
            "symbol": exch_symbol,
        }
        if exchange_order_id:
            params["orderId"] = exchange_order_id
        elif client_order_id:
            params["orderLinkId"] = client_order_id
        else:
            raise ValueError("Either exchange_order_id or client_order_id must be provided")

        # 1. Check realtime/active orders first
        realtime = await execute_with_retry(
            lambda: self._request("GET", "/v5/order/realtime", params=params, signed=True),
            rate_limiter=self._rate_limiter,
            weight=1.0,
        )
        items = realtime.get("list", [])
        if items:
            return self._parse_order_item(items[0], symbol, market_type)

        # 2. Check order history if already filled/canceled
        history = await execute_with_retry(
            lambda: self._request("GET", "/v5/order/history", params=params, signed=True),
            rate_limiter=self._rate_limiter,
            weight=1.0,
        )
        hist_items = history.get("list", [])
        if hist_items:
            return self._parse_order_item(hist_items[0], symbol, market_type)

        raise OrderNotFoundError(
            f"Order not found on Bybit (orderId={exchange_order_id}, orderLinkId={client_order_id})",
            exchange_id=ExchangeId.BYBIT,
        )

    async def fetch_open_orders(
        self, symbol: str | None, market_type: MarketType
    ) -> list[NormalizedOrderResponse]:
        category = _to_bybit_category(market_type)
        params: dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = _to_bybit_symbol(symbol)
        elif category == "linear":
            params["settleCoin"] = "USDT"

        res = await execute_with_retry(
            lambda: self._request("GET", "/v5/order/realtime", params=params, signed=True),
            rate_limiter=self._rate_limiter,
            weight=1.0,
        )
        results: list[NormalizedOrderResponse] = []
        for item in res.get("list", []):
            raw_sym = str(item.get("symbol", ""))
            unified_sym = symbol or (
                f"{raw_sym[:-4]}/USDT" if raw_sym.endswith("USDT") else raw_sym
            )
            results.append(self._parse_order_item(item, unified_sym, market_type))
        return results

    async def fetch_open_positions(
        self, market_type: MarketType, symbol: str | None = None
    ) -> list[ExchangePositionSnapshot]:
        if market_type != MarketType.FUTURES_USDT:
            return []
        params: dict[str, Any] = {"category": "linear"}
        if symbol:
            params["symbol"] = _to_bybit_symbol(symbol)
        else:
            params["settleCoin"] = "USDT"

        res = await execute_with_retry(
            lambda: self._request("GET", "/v5/position/list", params=params, signed=True),
            rate_limiter=self._rate_limiter,
            weight=1.0,
        )
        positions: list[ExchangePositionSnapshot] = []
        for row in res.get("list", []):
            size = to_decimal(row.get("size", "0"))
            if size <= Decimal("0"):
                continue
            raw_side = str(row.get("side", "Buy")).upper()
            side = PositionSide.LONG if raw_side == "BUY" else PositionSide.SHORT
            raw_sym = str(row.get("symbol", ""))
            unified_sym = symbol or (
                f"{raw_sym[:-4]}/USDT" if raw_sym.endswith("USDT") else raw_sym
            )
            trade_mode = int(row.get("tradeMode", 1))
            liq_raw = to_decimal(row.get("liqPrice") or "0")
            positions.append(
                ExchangePositionSnapshot(
                    symbol=unified_sym,
                    market_type=MarketType.FUTURES_USDT,
                    side=side,
                    quantity=size,
                    entry_price=to_decimal(row.get("avgPrice", "0")),
                    mark_price=to_decimal(row.get("markPrice", "0")),
                    unrealized_pnl=to_decimal(row.get("unrealisedPnl", "0")),
                    leverage=int(float(row.get("leverage", 1))),
                    margin_mode=MarginMode.ISOLATED if trade_mode == 1 else MarginMode.CROSS,
                    liquidation_price=liq_raw if liq_raw > Decimal("0") else None,
                )
            )
        return positions

    async def close_position_market(
        self,
        symbol: str,
        market_type: MarketType,
        side: PositionSide,
        quantity: Decimal,
        client_order_id: str,
    ) -> NormalizedOrderResponse:
        close_side = OrderSide.SELL if side == PositionSide.LONG else OrderSide.BUY
        req = NormalizedOrderRequest(
            client_order_id=client_order_id,
            symbol=symbol,
            market_type=market_type,
            side=close_side,
            order_type=OrderType.MARKET,
            purpose=OrderPurpose.EMERGENCY_CLOSE,
            quantity=quantity,
            reduce_only=(market_type == MarketType.FUTURES_USDT),
        )
        return await self.place_order(req)

    async def close(self) -> None:
        if not self._external_client:
            await self._client.aclose()
