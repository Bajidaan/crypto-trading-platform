"""Binance Spot & USDT-M Perpetual Futures Exchange Adapter."""

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
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
    NormalizedOrderFill,
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


def _to_binance_symbol(symbol: str) -> str:
    """Convert unified 'BTC/USDT' to Binance 'BTCUSDT'."""
    return symbol.replace("/", "").replace(":USDT", "").upper().strip()


def _to_unified_symbol(base: str, quote: str) -> str:
    return f"{base.upper()}/{quote.upper()}"


def _map_binance_order_status(raw_status: str) -> OrderStatus:
    status_map = {
        "NEW": OrderStatus.OPEN,
        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
        "FILLED": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELED,
        "PENDING_CANCEL": OrderStatus.OPEN,
        "REJECTED": OrderStatus.REJECTED,
        "EXPIRED": OrderStatus.EXPIRED,
        "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
    }
    return status_map.get(raw_status.upper(), OrderStatus.OPEN)


def _map_binance_order_type(raw_type: str) -> OrderType:
    type_map = {
        "MARKET": OrderType.MARKET,
        "LIMIT": OrderType.LIMIT,
        "STOP_MARKET": OrderType.STOP_MARKET,
        "STOP": OrderType.STOP_LIMIT,
        "STOP_LOSS": OrderType.STOP_MARKET,
        "STOP_LOSS_LIMIT": OrderType.STOP_LIMIT,
        "TAKE_PROFIT_MARKET": OrderType.TAKE_PROFIT_MARKET,
        "TAKE_PROFIT": OrderType.TAKE_PROFIT_LIMIT,
        "TAKE_PROFIT_LIMIT": OrderType.TAKE_PROFIT_LIMIT,
    }
    return type_map.get(raw_type.upper(), OrderType.LIMIT)


class BinanceAdapter(IExchangeAdapter):
    """Asynchronous Binance adapter supporting both Spot and USDT-M Perpetual Futures."""

    SPOT_LIVE_URL = "https://api.binance.com"
    SPOT_TESTNET_URL = "https://testnet.binance.vision"
    FUTURES_LIVE_URL = "https://fapi.binance.com"
    FUTURES_TESTNET_URL = "https://testnet.binancefuture.com"

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
            capacity=40.0, refill_rate_per_sec=20.0
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
        return ExchangeId.BINANCE

    @property
    def is_testnet(self) -> bool:
        return self._is_testnet

    def _base_url(self, market_type: MarketType) -> str:
        if market_type == MarketType.SPOT:
            return self.SPOT_TESTNET_URL if self._is_testnet else self.SPOT_LIVE_URL
        return self.FUTURES_TESTNET_URL if self._is_testnet else self.FUTURES_LIVE_URL

    def _sign_params(self, params: dict[str, Any]) -> str:
        query_string = urlencode(params, doseq=True)
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query_string}&signature={signature}"

    def _raise_mapped_error(self, status_code: int, payload: Any) -> None:
        code = payload.get("code") if isinstance(payload, dict) else None
        msg = payload.get("msg", str(payload)) if isinstance(payload, dict) else str(payload)

        if status_code in (429, 418) or code == -1003:
            raise ExchangeRateLimitError(
                f"Binance rate limit exceeded ({code}): {msg}",
                retry_after_seconds=5.0,
                exchange_id=ExchangeId.BINANCE,
                error_code=code or status_code,
            )
        if status_code in (401, 403) or code in (-1021, -1022, -2014, -2015):
            raise ExchangeAuthenticationError(
                f"Binance authentication error ({code}): {msg}",
                exchange_id=ExchangeId.BINANCE,
                error_code=code or status_code,
            )
        if code in (-2010, -2019) and ("insufficient" in msg.lower() or code == -2019):
            raise ExchangeInsufficientFundsError(
                f"Binance insufficient funds ({code}): {msg}",
                exchange_id=ExchangeId.BINANCE,
                error_code=code,
            )
        if code in (-2011, -2013):
            raise OrderNotFoundError(
                f"Binance order not found ({code}): {msg}",
                exchange_id=ExchangeId.BINANCE,
                error_code=code,
            )
        if code in (-1013, -1111, -1121, -2010, -2021, -4061, -4164):
            raise ExchangeOrderRejectedError(
                f"Binance order rejected ({code}): {msg}",
                exchange_id=ExchangeId.BINANCE,
                error_code=code,
            )
        if status_code >= 500:
            raise ExchangeNetworkError(
                f"Binance 5xx server error ({status_code}): {msg}",
                exchange_id=ExchangeId.BINANCE,
                error_code=status_code,
            )
        raise ExchangeAdapterError(
            f"Binance API error (HTTP {status_code}, code={code}): {msg}",
            exchange_id=ExchangeId.BINANCE,
            error_code=code or status_code,
        )

    async def _request(
        self,
        method: str,
        market_type: MarketType,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        req_params = {k: v for k, v in (params or {}).items() if v is not None}
        headers: dict[str, str] = {}
        if signed:
            headers["X-MBX-APIKEY"] = self._api_key
            req_params["timestamp"] = self._time_ms_fn()
            req_params.setdefault("recvWindow", 5000)
            query = self._sign_params(req_params)
            url = f"{self._base_url(market_type)}{path}?{query}"
        else:
            query = urlencode(req_params, doseq=True)
            url = f"{self._base_url(market_type)}{path}" + (f"?{query}" if query else "")

        try:
            response = await self._client.request(method=method.upper(), url=url, headers=headers)
        except httpx.RequestError as exc:
            raise ExchangeNetworkError(
                f"Binance network request failed ({method} {path}): {exc}",
                exchange_id=ExchangeId.BINANCE,
            ) from exc

        try:
            payload = response.json()
        except Exception:
            payload = response.text

        if response.status_code >= 400 or (
            isinstance(payload, dict) and "code" in payload and int(payload["code"]) < 0
        ):
            self._raise_mapped_error(response.status_code, payload)

        return payload

    async def load_markets(
        self, market_type: MarketType, force_refresh: bool = False
    ) -> dict[str, MarketRules]:
        if self._markets_cache[market_type] and not force_refresh:
            return self._markets_cache[market_type]

        path = "/api/v3/exchangeInfo" if market_type == MarketType.SPOT else "/fapi/v1/exchangeInfo"
        data = await execute_with_retry(
            lambda: self._request("GET", market_type, path, signed=False),
            rate_limiter=self._rate_limiter,
            weight=10.0,
        )

        rules_map: dict[str, MarketRules] = {}
        for item in data.get("symbols", []):
            status = item.get("status", "TRADING")
            if status != "TRADING":
                continue
            base_asset = item.get("baseAsset", "")
            quote_asset = item.get("quoteAsset", "")
            if not base_asset or not quote_asset:
                continue

            unified_symbol = _to_unified_symbol(base_asset, quote_asset)
            exch_symbol = item.get("symbol", f"{base_asset}{quote_asset}")

            tick_size = Decimal("0.01")
            step_size = Decimal("0.001")
            min_qty = Decimal("0.001")
            max_qty = Decimal("1000000")
            min_notional = Decimal("5.0")

            for flt in item.get("filters", []):
                ftype = flt.get("filterType")
                if ftype == "PRICE_FILTER":
                    tick_size = to_decimal(flt.get("tickSize", "0.01"))
                elif ftype == "LOT_SIZE":
                    step_size = to_decimal(flt.get("stepSize", "0.001"))
                    min_qty = to_decimal(flt.get("minQty", "0.001"))
                    max_qty = to_decimal(flt.get("maxQty", "1000000"))
                elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                    raw_notional = flt.get("minNotional") or flt.get("notional") or "5.0"
                    min_notional = to_decimal(raw_notional)

            rules_map[unified_symbol] = MarketRules(
                symbol=unified_symbol,
                exchange_symbol=exch_symbol,
                market_type=market_type,
                base_asset=base_asset,
                quote_asset=quote_asset,
                tick_size=tick_size,
                step_size=step_size,
                min_qty=min_qty,
                max_qty=max_qty,
                min_notional=min_notional,
                maker_fee_rate=Decimal("0.001") if market_type == MarketType.SPOT else Decimal("0.0002"),
                taker_fee_rate=Decimal("0.001") if market_type == MarketType.SPOT else Decimal("0.0005"),
                max_leverage=self._max_leverage_cap if market_type == MarketType.FUTURES_USDT else 1,
            )

        self._markets_cache[market_type] = rules_map
        return rules_map

    async def get_market_rules(self, symbol: str, market_type: MarketType) -> MarketRules:
        markets = await self.load_markets(market_type)
        if symbol not in markets:
            raise ExchangeAdapterError(
                f"Symbol {symbol} not found in Binance {market_type.value} markets",
                exchange_id=ExchangeId.BINANCE,
            )
        return markets[symbol]

    async def fetch_balance(self, market_type: MarketType) -> AccountBalance:
        if market_type == MarketType.SPOT:
            data = await execute_with_retry(
                lambda: self._request("GET", MarketType.SPOT, "/api/v3/account", signed=True),
                rate_limiter=self._rate_limiter,
                weight=10.0,
            )
            free_usdt = Decimal("0")
            locked_usdt = Decimal("0")
            for bal in data.get("balances", []):
                if bal.get("asset") == "USDT":
                    free_usdt = to_decimal(bal.get("free", "0"))
                    locked_usdt = to_decimal(bal.get("locked", "0"))
                    break
            return AccountBalance(
                exchange_id=ExchangeId.BINANCE,
                market_type=MarketType.SPOT,
                currency="USDT",
                total_equity=free_usdt + locked_usdt,
                available_balance=free_usdt,
                used_margin=locked_usdt,
                unrealized_pnl=Decimal("0"),
            )

        data = await execute_with_retry(
            lambda: self._request("GET", MarketType.FUTURES_USDT, "/fapi/v2/account", signed=True),
            rate_limiter=self._rate_limiter,
            weight=5.0,
        )
        total_margin_balance = to_decimal(data.get("totalMarginBalance", "0"))
        available_balance = to_decimal(data.get("availableBalance", "0"))
        used_margin = to_decimal(data.get("totalInitialMargin", "0"))
        unrealized_pnl = to_decimal(data.get("totalUnrealizedProfit", "0"))
        return AccountBalance(
            exchange_id=ExchangeId.BINANCE,
            market_type=MarketType.FUTURES_USDT,
            currency="USDT",
            total_equity=total_margin_balance,
            available_balance=available_balance,
            used_margin=used_margin,
            unrealized_pnl=unrealized_pnl,
        )

    async def fetch_ticker(self, symbol: str, market_type: MarketType) -> TickerSnapshot:
        exch_symbol = _to_binance_symbol(symbol)
        if market_type == MarketType.SPOT:
            data = await execute_with_retry(
                lambda: self._request(
                    "GET",
                    MarketType.SPOT,
                    "/api/v3/ticker/24hr",
                    params={"symbol": exch_symbol},
                ),
                rate_limiter=self._rate_limiter,
                weight=2.0,
            )
            last_price = to_decimal(data.get("lastPrice", "0"))
            bid = to_decimal(data.get("bidPrice", str(last_price)))
            ask = to_decimal(data.get("askPrice", str(last_price)))
            vol_usd = to_decimal(data.get("quoteVolume", "0"))
            return TickerSnapshot(
                symbol=symbol,
                market_type=MarketType.SPOT,
                bid=bid,
                ask=ask,
                last_price=last_price,
                mark_price=last_price,
                volume_24h_usd=vol_usd,
            )

        book = await execute_with_retry(
            lambda: self._request(
                "GET",
                MarketType.FUTURES_USDT,
                "/fapi/v1/ticker/bookTicker",
                params={"symbol": exch_symbol},
            ),
            rate_limiter=self._rate_limiter,
            weight=2.0,
        )
        prem = await execute_with_retry(
            lambda: self._request(
                "GET",
                MarketType.FUTURES_USDT,
                "/fapi/v1/premiumIndex",
                params={"symbol": exch_symbol},
            ),
            rate_limiter=self._rate_limiter,
            weight=1.0,
        )
        bid = to_decimal(book.get("bidPrice", "0"))
        ask = to_decimal(book.get("askPrice", "0"))
        mark_price = to_decimal(prem.get("markPrice", str((bid + ask) / Decimal("2"))))
        last_price = to_decimal(prem.get("indexPrice", str(mark_price)))
        return TickerSnapshot(
            symbol=symbol,
            market_type=MarketType.FUTURES_USDT,
            bid=bid,
            ask=ask,
            last_price=last_price,
            mark_price=mark_price,
        )

    async def fetch_ohlcv(
        self,
        symbol: str,
        market_type: MarketType,
        timeframe: str,
        since_ms: int | None = None,
        limit: int = 500,
    ) -> list[CandleBar]:
        exch_symbol = _to_binance_symbol(symbol)
        path = "/api/v3/klines" if market_type == MarketType.SPOT else "/fapi/v1/klines"
        params: dict[str, Any] = {
            "symbol": exch_symbol,
            "interval": timeframe,
            "limit": min(limit, 1000),
        }
        if since_ms is not None:
            params["startTime"] = since_ms

        rows = await execute_with_retry(
            lambda: self._request("GET", market_type, path, params=params),
            rate_limiter=self._rate_limiter,
            weight=2.0,
        )
        bars: list[CandleBar] = []
        now_ms = self._time_ms_fn()
        for row in rows:
            open_ms = int(row[0])
            close_ms = int(row[6]) if len(row) > 6 else open_ms
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
                    is_closed=close_ms < now_ms,
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
        exch_symbol = _to_binance_symbol(symbol)
        capped_leverage = max(1, min(int(leverage), self._max_leverage_cap))

        # 1. Enforce One-Way Position Mode (dualSidePosition = false)
        try:
            await execute_with_retry(
                lambda: self._request(
                    "POST",
                    MarketType.FUTURES_USDT,
                    "/fapi/v1/positionSide/dual",
                    params={"dualSidePosition": "false" if one_way_mode else "true"},
                    signed=True,
                ),
                rate_limiter=self._rate_limiter,
            )
        except ExchangeAdapterError as exc:
            # -4059: "No need to change position side."
            if exc.error_code != -4059:
                raise

        # 2. Enforce Isolated Margin Mode
        target_margin = "ISOLATED" if margin_mode == MarginMode.ISOLATED else "CROSSED"
        try:
            await execute_with_retry(
                lambda: self._request(
                    "POST",
                    MarketType.FUTURES_USDT,
                    "/fapi/v1/marginType",
                    params={"symbol": exch_symbol, "marginType": target_margin},
                    signed=True,
                ),
                rate_limiter=self._rate_limiter,
            )
        except ExchangeAdapterError as exc:
            # -4046: "No need to change margin type."
            if exc.error_code != -4046:
                raise

        # 3. Set Capped Leverage
        await execute_with_retry(
            lambda: self._request(
                "POST",
                MarketType.FUTURES_USDT,
                "/fapi/v1/leverage",
                params={"symbol": exch_symbol, "leverage": capped_leverage},
                signed=True,
            ),
            rate_limiter=self._rate_limiter,
        )

    def _parse_order_response(
        self, raw: dict[str, Any], symbol: str, market_type: MarketType
    ) -> NormalizedOrderResponse:
        client_oid = str(
            raw.get("clientOrderId") or raw.get("origClientOrderId") or ""
        )
        exch_oid = str(raw.get("orderId", ""))
        side = OrderSide.BUY if str(raw.get("side", "BUY")).upper() == "BUY" else OrderSide.SELL
        order_type = _map_binance_order_type(str(raw.get("type", "MARKET")))
        status = _map_binance_order_status(str(raw.get("status", "NEW")))

        req_qty = to_decimal(raw.get("origQty", "0"))
        filled_qty = to_decimal(raw.get("executedQty", "0"))
        raw_price = to_decimal(raw.get("price", "0"))
        raw_stop = to_decimal(raw.get("stopPrice", "0"))

        avg_price: Decimal | None = None
        if raw.get("avgPrice") and to_decimal(raw["avgPrice"]) > Decimal("0"):
            avg_price = to_decimal(raw["avgPrice"])
        elif filled_qty > Decimal("0") and raw.get("cummulativeQuoteQty"):
            cum_quote = to_decimal(raw["cummulativeQuoteQty"])
            if cum_quote > Decimal("0"):
                avg_price = cum_quote / filled_qty
        elif status == OrderStatus.FILLED and raw_price > Decimal("0"):
            avg_price = raw_price

        fills: list[NormalizedOrderFill] = []
        total_fee_usd = Decimal("0")
        for f in raw.get("fills", []):
            f_price = to_decimal(f.get("price", "0"))
            f_qty = to_decimal(f.get("qty", "0"))
            f_fee = to_decimal(f.get("commission", "0"))
            f_asset = str(f.get("commissionAsset", "USDT"))
            total_fee_usd += f_fee
            fills.append(
                NormalizedOrderFill(
                    exchange_trade_id=str(f.get("tradeId", "")),
                    fill_price=f_price,
                    fill_qty=f_qty,
                    fee_amount=f_fee,
                    fee_currency=f_asset,
                )
            )

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
            avg_fill_price=avg_price,
            price=raw_price if raw_price > Decimal("0") else None,
            stop_price=raw_stop if raw_stop > Decimal("0") else None,
            fee_usd=total_fee_usd,
            reduce_only=bool(raw.get("reduceOnly", False)),
            fills=fills,
            raw_response=raw,
        )

    async def place_order(self, request: NormalizedOrderRequest) -> NormalizedOrderResponse:
        if not CLIENT_ORDER_ID_PATTERN.match(request.client_order_id):
            raise ExchangeOrderRejectedError(
                f"Invalid client_order_id '{request.client_order_id}'",
                exchange_id=ExchangeId.BINANCE,
            )

        rules = await self.get_market_rules(request.symbol, request.market_type)
        q_qty = rules.format_quantity(request.quantity)
        q_price = rules.format_price(request.price) if request.price is not None else None
        q_stop = rules.format_price(request.stop_price) if request.stop_price is not None else None

        # Validate min_qty (and min_notional if price or stop_price is known and not reduce_only)
        if q_qty < rules.min_qty:
            raise ExchangeOrderRejectedError(
                f"Quantized quantity {q_qty} is below minimum {rules.min_qty} for {request.symbol}",
                exchange_id=ExchangeId.BINANCE,
            )
        ref_price = q_price or q_stop
        if ref_price is not None and not request.reduce_only:
            ok, err = rules.validate_order(price=ref_price, quantity=q_qty)
            if not ok:
                raise ExchangeOrderRejectedError(
                    err or "Order failed filter validation",
                    exchange_id=ExchangeId.BINANCE,
                )

        exch_symbol = rules.exchange_symbol
        path = "/api/v3/order" if request.market_type == MarketType.SPOT else "/fapi/v1/order"
        params: dict[str, Any] = {
            "symbol": exch_symbol,
            "side": request.side.value,
            "newClientOrderId": request.client_order_id,
            "quantity": str(q_qty),
        }
        if request.market_type == MarketType.SPOT:
            params["newOrderRespType"] = "FULL"
        else:
            params["newOrderRespType"] = "RESULT"

        if request.order_type == OrderType.MARKET:
            params["type"] = "MARKET"
        elif request.order_type == OrderType.LIMIT:
            if q_price is None:
                raise ExchangeOrderRejectedError("LIMIT order requires price", exchange_id=ExchangeId.BINANCE)
            params["type"] = "LIMIT"
            params["price"] = str(q_price)
            params["timeInForce"] = request.time_in_force
        elif request.order_type == OrderType.STOP_MARKET:
            if q_stop is None:
                raise ExchangeOrderRejectedError("STOP_MARKET order requires stop_price", exchange_id=ExchangeId.BINANCE)
            if request.market_type == MarketType.SPOT:
                params["type"] = "STOP_LOSS"
                params["stopPrice"] = str(q_stop)
            else:
                params["type"] = "STOP_MARKET"
                params["stopPrice"] = str(q_stop)
                params["workingType"] = request.working_type
        elif request.order_type == OrderType.STOP_LIMIT:
            if q_stop is None or q_price is None:
                raise ExchangeOrderRejectedError("STOP_LIMIT order requires stop_price and price", exchange_id=ExchangeId.BINANCE)
            if request.market_type == MarketType.SPOT:
                params["type"] = "STOP_LOSS_LIMIT"
            else:
                params["type"] = "STOP"
                params["workingType"] = request.working_type
            params["stopPrice"] = str(q_stop)
            params["price"] = str(q_price)
            params["timeInForce"] = request.time_in_force
        elif request.order_type == OrderType.TAKE_PROFIT_MARKET:
            if q_stop is None:
                raise ExchangeOrderRejectedError("TAKE_PROFIT_MARKET requires stop_price", exchange_id=ExchangeId.BINANCE)
            params["type"] = "TAKE_PROFIT_MARKET"
            params["stopPrice"] = str(q_stop)
            params["workingType"] = request.working_type

        if request.reduce_only and request.market_type == MarketType.FUTURES_USDT:
            params["reduceOnly"] = "true"

        async def _submit() -> NormalizedOrderResponse:
            raw = await self._request("POST", request.market_type, path, params=params, signed=True)
            return self._parse_order_response(raw, request.symbol, request.market_type)

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
            )
            return await self.place_order(req)

        # Spot market: place STOP_LOSS_LIMIT with a 0.3% aggressive limit buffer if limit_price not specified
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
        exch_symbol = _to_binance_symbol(symbol)
        path = "/api/v3/order" if market_type == MarketType.SPOT else "/fapi/v1/order"
        params: dict[str, Any] = {"symbol": exch_symbol}
        if exchange_order_id:
            params["orderId"] = exchange_order_id
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            raise ValueError("Either exchange_order_id or client_order_id must be provided")

        await execute_with_retry(
            lambda: self._request("DELETE", market_type, path, params=params, signed=True),
            rate_limiter=self._rate_limiter,
            weight=1.0,
        )
        return True

    async def cancel_all_orders(self, symbol: str, market_type: MarketType) -> int:
        exch_symbol = _to_binance_symbol(symbol)
        path = "/api/v3/openOrders" if market_type == MarketType.SPOT else "/fapi/v1/allOpenOrders"
        res = await execute_with_retry(
            lambda: self._request("DELETE", market_type, path, params={"symbol": exch_symbol}, signed=True),
            rate_limiter=self._rate_limiter,
            weight=1.0,
        )
        if isinstance(res, list):
            return len(res)
        return 1

    async def fetch_order(
        self,
        symbol: str,
        market_type: MarketType,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> NormalizedOrderResponse:
        exch_symbol = _to_binance_symbol(symbol)
        path = "/api/v3/order" if market_type == MarketType.SPOT else "/fapi/v1/order"
        params: dict[str, Any] = {"symbol": exch_symbol}
        if exchange_order_id:
            params["orderId"] = exchange_order_id
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            raise ValueError("Either exchange_order_id or client_order_id must be provided")

        raw = await execute_with_retry(
            lambda: self._request("GET", market_type, path, params=params, signed=True),
            rate_limiter=self._rate_limiter,
            weight=2.0,
        )
        return self._parse_order_response(raw, symbol, market_type)

    async def fetch_open_orders(
        self, symbol: str | None, market_type: MarketType
    ) -> list[NormalizedOrderResponse]:
        path = "/api/v3/openOrders" if market_type == MarketType.SPOT else "/fapi/v1/openOrders"
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = _to_binance_symbol(symbol)
        rows = await execute_with_retry(
            lambda: self._request("GET", market_type, path, params=params, signed=True),
            rate_limiter=self._rate_limiter,
            weight=3.0,
        )
        results: list[NormalizedOrderResponse] = []
        for row in rows:
            sym = symbol or str(row.get("symbol", ""))
            results.append(self._parse_order_response(row, sym, market_type))
        return results

    async def fetch_open_positions(
        self, market_type: MarketType, symbol: str | None = None
    ) -> list[ExchangePositionSnapshot]:
        if market_type != MarketType.FUTURES_USDT:
            return []
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = _to_binance_symbol(symbol)

        rows = await execute_with_retry(
            lambda: self._request(
                "GET", MarketType.FUTURES_USDT, "/fapi/v2/positionRisk", params=params, signed=True
            ),
            rate_limiter=self._rate_limiter,
            weight=5.0,
        )
        positions: list[ExchangePositionSnapshot] = []
        for row in rows:
            amt = to_decimal(row.get("positionAmt", "0"))
            if amt == Decimal("0"):
                continue
            side = PositionSide.LONG if amt > Decimal("0") else PositionSide.SHORT
            raw_sym = str(row.get("symbol", ""))
            unified_sym = symbol if symbol else (
                f"{raw_sym[:-4]}/USDT" if raw_sym.endswith("USDT") else raw_sym
            )
            m_type = str(row.get("marginType", "isolated")).upper()
            liq = to_decimal(row.get("liquidationPrice", "0"))
            positions.append(
                ExchangePositionSnapshot(
                    symbol=unified_sym,
                    market_type=MarketType.FUTURES_USDT,
                    side=side,
                    quantity=abs(amt),
                    entry_price=to_decimal(row.get("entryPrice", "0")),
                    mark_price=to_decimal(row.get("markPrice", "0")),
                    unrealized_pnl=to_decimal(row.get("unRealizedProfit", "0")),
                    leverage=int(row.get("leverage", 1)),
                    margin_mode=MarginMode.ISOLATED if m_type == "ISOLATED" else MarginMode.CROSS,
                    liquidation_price=liq if liq > Decimal("0") else None,
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
