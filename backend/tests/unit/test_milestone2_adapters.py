"""Milestone 2 Test Suite: Token-Bucket Rate Limiter, Idempotent Retry, Binance & Bybit Adapters."""

from decimal import Decimal
import json
from typing import Any
from urllib.parse import parse_qs, urlparse
import httpx
import pytest

from app.core.security import CredentialVault, mask_api_key
from app.domains.cex.adapters import (
    AsyncTokenBucketRateLimiter,
    BinanceAdapter,
    BybitAdapter,
    ExchangeAdapterFactory,
    ExchangeAuthenticationError,
    ExchangeNetworkError,
    ExchangeOrderRejectedError,
    ExchangeRateLimitError,
    NormalizedOrderRequest,
    NormalizedOrderResponse,
    OrderNotFoundError,
    execute_with_retry,
    submit_order_idempotently,
)
from app.models.entities import ExchangeConnection
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


# =====================================================================
# 1. Token-Bucket Rate Limiter & Idempotent Retry Executor Tests
# =====================================================================


async def test_token_bucket_rate_limiter_acquire_and_cooldown() -> None:
    clock = [100.0]
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(round(seconds, 4))
        clock[0] += seconds

    limiter = AsyncTokenBucketRateLimiter(
        capacity=10.0,
        refill_rate_per_sec=5.0,
        time_fn=lambda: clock[0],
        sleep_fn=fake_sleep,
    )

    # Acquire 8 tokens immediately (2 left)
    waited_1 = await limiter.acquire(8.0)
    assert waited_1 == 0.0
    assert limiter.available_tokens == pytest.approx(2.0)

    # Acquire 7 tokens -> needs 5 more tokens at 5/sec = 1.0s sleep
    waited_2 = await limiter.acquire(7.0)
    assert waited_2 == pytest.approx(1.0)
    assert slept == [1.0]

    # Penalize cooldown for 4.0 seconds (e.g. HTTP 429)
    limiter.penalize_cooldown(4.0)
    waited_3 = await limiter.acquire(5.0)
    # 4.0s cooldown refills 20 tokens (capped at 10.0), so no extra wait needed beyond 4.0s
    assert waited_3 == pytest.approx(4.0)
    assert limiter.stats["cooldown_events"] == 1


async def test_execute_with_retry_handles_rate_limit_and_network_errors() -> None:
    attempts = 0
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    async def flaky_op() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ExchangeRateLimitError("429 Too Many Requests", retry_after_seconds=2.0)
        if attempts == 2:
            raise ExchangeNetworkError("Connection reset by peer")
        return "SUCCESS"

    result = await execute_with_retry(flaky_op, max_retries=3, sleep_fn=fake_sleep)
    assert result == "SUCCESS"
    assert attempts == 3
    assert len(slept) == 2


async def test_submit_order_idempotently_prevents_double_order_on_timeout() -> None:
    """Verify that if order POST times out, we query clientOrderId first and do NOT re-POST if matched."""
    submit_calls = 0
    query_calls = 0

    async def fake_sleep(_s: float) -> None:
        return None

    async def submit_that_timed_out_after_reaching_exchange() -> NormalizedOrderResponse:
        nonlocal submit_calls
        submit_calls += 1
        raise ExchangeNetworkError("ReadTimeout waiting for Binance order response")

    async def query_existing_finds_order() -> NormalizedOrderResponse:
        nonlocal query_calls
        query_calls += 1
        return NormalizedOrderResponse(
            client_order_id="T_ENT_BTCUSDT_001",
            exchange_order_id="99887766",
            symbol="BTC/USDT",
            market_type=MarketType.FUTURES_USDT,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            requested_qty=Decimal("0.05"),
            filled_qty=Decimal("0.05"),
            avg_fill_price=Decimal("65000.00"),
        )

    recovered = await submit_order_idempotently(
        submit_fn=submit_that_timed_out_after_reaching_exchange,
        query_existing_fn=query_existing_finds_order,
        sleep_fn=fake_sleep,
    )
    assert recovered.exchange_order_id == "99887766"
    assert recovered.status == OrderStatus.FILLED
    # Crucial invariant: submit_fn was called ONLY ONCE!
    assert submit_calls == 1
    assert query_calls == 1


async def test_submit_order_idempotently_resubmits_when_order_not_found() -> None:
    """Verify that if order POST times out BEFORE reaching the exchange, we re-POST after OrderNotFoundError."""
    submit_calls = 0
    query_calls = 0

    async def fake_sleep(_s: float) -> None:
        return None

    async def submit_fn() -> NormalizedOrderResponse:
        nonlocal submit_calls
        submit_calls += 1
        if submit_calls == 1:
            raise ExchangeNetworkError("ConnectTimeout before packet sent")
        return NormalizedOrderResponse(
            client_order_id="T_ENT_BTCUSDT_002",
            exchange_order_id="11223344",
            symbol="BTC/USDT",
            market_type=MarketType.SPOT,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            requested_qty=Decimal("0.01"),
            filled_qty=Decimal("0.01"),
            avg_fill_price=Decimal("64000.00"),
        )

    async def query_existing_fn() -> NormalizedOrderResponse:
        nonlocal query_calls
        query_calls += 1
        raise OrderNotFoundError("Order does not exist")

    resp = await submit_order_idempotently(
        submit_fn=submit_fn,
        query_existing_fn=query_existing_fn,
        sleep_fn=fake_sleep,
    )
    assert resp.exchange_order_id == "11223344"
    assert submit_calls == 2
    assert query_calls == 1


# =====================================================================
# 2. Binance Adapter (Spot & USDT-M Perpetual Futures) Tests
# =====================================================================


async def test_binance_adapter_markets_balance_futures_setup_and_orders() -> None:
    recorded_requests: list[httpx.Request] = []

    def mock_binance_handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        parsed = urlparse(str(request.url))
        path = parsed.path
        qs = parse_qs(parsed.query)

        # 1. Exchange Info
        if path in ("/api/v3/exchangeInfo", "/fapi/v1/exchangeInfo"):
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "status": "TRADING",
                            "baseAsset": "BTC",
                            "quoteAsset": "USDT",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                                {
                                    "filterType": "LOT_SIZE",
                                    "stepSize": "0.001",
                                    "minQty": "0.001",
                                    "maxQty": "100.0",
                                },
                                {"filterType": "MIN_NOTIONAL", "minNotional": "10.0"},
                            ],
                        }
                    ]
                },
            )

        # 2. Futures Account Balance
        if path == "/fapi/v2/account":
            assert request.headers.get("X-MBX-APIKEY") == "BINANCE_TEST_KEY"
            assert "signature" in qs
            return httpx.Response(
                200,
                json={
                    "totalMarginBalance": "5250.75",
                    "availableBalance": "4100.25",
                    "totalInitialMargin": "1150.50",
                    "totalUnrealizedProfit": "125.75",
                },
            )

        # 3. Futures Symbol Configuration (One-Way Mode, Isolated Margin, Capped Leverage)
        if path == "/fapi/v1/positionSide/dual":
            assert qs["dualSidePosition"][0] == "false"
            return httpx.Response(400, json={"code": -4059, "msg": "No need to change position side."})

        if path == "/fapi/v1/marginType":
            assert qs["symbol"][0] == "BTCUSDT"
            assert qs["marginType"][0] == "ISOLATED"
            return httpx.Response(400, json={"code": -4046, "msg": "No need to change margin type."})

        if path == "/fapi/v1/leverage":
            assert qs["symbol"][0] == "BTCUSDT"
            # Even if caller requested 25x, adapter caps at max_leverage_cap=10
            assert qs["leverage"][0] == "10"
            return httpx.Response(200, json={"symbol": "BTCUSDT", "leverage": 10})

        # 4. Order Placement (Spot & Futures)
        if path == "/fapi/v1/order" and request.method == "POST":
            otype = qs["type"][0]
            if otype == "STOP_MARKET":
                assert qs["reduceOnly"][0] == "true"
                assert qs["workingType"][0] == "MARK_PRICE"
                return httpx.Response(
                    200,
                    json={
                        "orderId": 50002,
                        "clientOrderId": qs["newClientOrderId"][0],
                        "symbol": "BTCUSDT",
                        "side": qs["side"][0],
                        "type": "STOP_MARKET",
                        "status": "NEW",
                        "origQty": qs["quantity"][0],
                        "executedQty": "0.000",
                        "stopPrice": qs["stopPrice"][0],
                        "reduceOnly": True,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "orderId": 50001,
                    "clientOrderId": qs["newClientOrderId"][0],
                    "symbol": "BTCUSDT",
                    "side": qs["side"][0],
                    "type": otype,
                    "status": "FILLED",
                    "origQty": qs["quantity"][0],
                    "executedQty": qs["quantity"][0],
                    "avgPrice": "65000.10",
                    "reduceOnly": False,
                },
            )

        if path == "/api/v3/order" and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "orderId": 60001,
                    "clientOrderId": qs["newClientOrderId"][0],
                    "symbol": "BTCUSDT",
                    "side": qs["side"][0],
                    "type": qs["type"][0],
                    "status": "NEW",
                    "origQty": qs["quantity"][0],
                    "executedQty": "0.000",
                    "price": qs.get("price", ["0"])[0],
                    "stopPrice": qs.get("stopPrice", ["0"])[0],
                },
            )

        if path == "/fapi/v2/positionRisk":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "0.015",
                        "entryPrice": "65000.10",
                        "markPrice": "65500.00",
                        "unRealizedProfit": "7.4985",
                        "leverage": "3",
                        "marginType": "isolated",
                        "liquidationPrice": "45120.50",
                    }
                ],
            )

        return httpx.Response(404, json={"code": -1000, "msg": f"Unhandled mock path {path}"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_binance_handler))
    adapter = BinanceAdapter(
        api_key="BINANCE_TEST_KEY",
        api_secret="BINANCE_TEST_SECRET",
        is_testnet=True,
        max_leverage_cap=10,
        http_client=client,
    )

    # 1. Load markets & check quantization rules
    rules = await adapter.get_market_rules("BTC/USDT", MarketType.FUTURES_USDT)
    assert rules.tick_size == Decimal("0.10")
    assert rules.step_size == Decimal("0.001")
    assert rules.min_notional == Decimal("10.0")

    # 2. Fetch Futures balance
    bal = await adapter.fetch_balance(MarketType.FUTURES_USDT)
    assert bal.total_equity == Decimal("5250.75")
    assert bal.available_balance == Decimal("4100.25")
    assert bal.unrealized_pnl == Decimal("125.75")

    # 3. Configure Futures symbol (requesting 25x leverage -> automatically capped at 10x)
    await adapter.configure_futures_symbol(
        symbol="BTC/USDT",
        leverage=25,
        margin_mode=MarginMode.ISOLATED,
        one_way_mode=True,
    )

    # 4. Place Futures entry order with unquantized quantity 0.0159 -> quantized DOWN to 0.015
    entry_req = NormalizedOrderRequest(
        client_order_id="T_ENT_BTC_1001",
        symbol="BTC/USDT",
        market_type=MarketType.FUTURES_USDT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        purpose=OrderPurpose.ENTRY,
        quantity=Decimal("0.0159"),
    )
    entry_resp = await adapter.place_order(entry_req)
    assert entry_resp.status == OrderStatus.FILLED
    assert entry_resp.requested_qty == Decimal("0.015")
    assert entry_resp.avg_fill_price == Decimal("65000.10")

    # 5. Place Tier-1 Hard Stop-Loss on Futures (STOP_MARKET + reduceOnly + MARK_PRICE)
    sl_resp = await adapter.place_hard_stop_loss(
        symbol="BTC/USDT",
        market_type=MarketType.FUTURES_USDT,
        side=OrderSide.SELL,
        quantity=Decimal("0.015"),
        stop_price=Decimal("63700.14"),  # Quantized to 63700.10 (tick_size=0.10)
        client_order_id="T_SL_BTC_1001",
    )
    assert sl_resp.order_type == OrderType.STOP_MARKET
    assert sl_resp.stop_price == Decimal("63700.10")
    assert sl_resp.reduce_only is True

    # 6. Place Tier-1 Hard Stop-Loss on Spot (STOP_LOSS_LIMIT with 0.3% buffer)
    spot_sl = await adapter.place_hard_stop_loss(
        symbol="BTC/USDT",
        market_type=MarketType.SPOT,
        side=OrderSide.SELL,
        quantity=Decimal("0.015"),
        stop_price=Decimal("63700.00"),
        client_order_id="T_SL_SPOT_1001",
    )
    assert spot_sl.order_type == OrderType.STOP_LIMIT
    assert spot_sl.stop_price == Decimal("63700.00")
    assert spot_sl.price == Decimal("63508.90")  # 63700 * 0.997 quantized to 0.10 tick

    # 7. Fetch open Futures positions
    positions = await adapter.fetch_open_positions(MarketType.FUTURES_USDT, "BTC/USDT")
    assert len(positions) == 1
    assert positions[0].side == PositionSide.LONG
    assert positions[0].quantity == Decimal("0.015")
    assert positions[0].margin_mode == MarginMode.ISOLATED

    # 8. Reject order below min_qty before hitting the network
    with pytest.raises(ExchangeOrderRejectedError, match="below minimum"):
        await adapter.place_order(
            NormalizedOrderRequest(
                client_order_id="T_TINY_001",
                symbol="BTC/USDT",
                market_type=MarketType.FUTURES_USDT,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("0.0004"),
            )
        )

    await client.aclose()


# =====================================================================
# 3. Bybit v5 Adapter (Spot & USDT Linear Perpetual) Tests
# =====================================================================


async def test_bybit_adapter_v5_signing_futures_setup_and_atomic_sl_amend() -> None:
    amend_called = False

    def mock_bybit_handler(request: httpx.Request) -> httpx.Response:
        nonlocal amend_called
        parsed = urlparse(str(request.url))
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/v5/market/instruments-info":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "list": [
                            {
                                "symbol": "ETHUSDT",
                                "status": "Trading",
                                "baseCoin": "ETH",
                                "quoteCoin": "USDT",
                                "priceFilter": {"tickSize": "0.05"},
                                "lotSizeFilter": {
                                    "qtyStep": "0.01",
                                    "minOrderQty": "0.01",
                                    "maxOrderQty": "1000",
                                    "minNotionalValue": "5.0",
                                },
                                "leverageFilter": {"maxLeverage": "50"},
                            }
                        ]
                    },
                },
            )

        if path == "/v5/account/wallet-balance":
            assert request.headers.get("X-BAPI-API-KEY") == "BYBIT_TEST_KEY"
            assert request.headers.get("X-BAPI-SIGN") is not None
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "list": [
                            {
                                "totalEquity": "10000.00",
                                "totalAvailableBalance": "8500.00",
                                "totalInitialMargin": "1500.00",
                                "totalPerpUPL": "42.50",
                                "coin": [
                                    {
                                        "coin": "USDT",
                                        "equity": "10000.00",
                                        "walletBalance": "9957.50",
                                        "availableToWithdraw": "8500.00",
                                        "totalPositionIM": "1500.00",
                                        "unrealisedPnl": "42.50",
                                    }
                                ],
                            }
                        ]
                    },
                },
            )

        if path == "/v5/market/kline":
            # Bybit returns newest candle first; adapter must sort oldest first
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "list": [
                            ["1700000900000", "3110.0", "3125.0", "3105.0", "3120.0", "450.5"],
                            ["1700000000000", "3100.0", "3115.0", "3095.0", "3110.0", "380.2"],
                        ]
                    },
                },
            )

        if path == "/v5/position/switch-mode":
            body = json.loads(request.content.decode("utf-8"))
            assert body["mode"] == 0
            return httpx.Response(200, json={"retCode": 110025, "retMsg": "Position mode is not modified"})

        if path == "/v5/position/switch-isolated":
            body = json.loads(request.content.decode("utf-8"))
            assert body["tradeMode"] == 1
            return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {}})

        if path == "/v5/position/set-leverage":
            body = json.loads(request.content.decode("utf-8"))
            assert body["buyLeverage"] == "5"
            assert body["sellLeverage"] == "5"
            return httpx.Response(200, json={"retCode": 110043, "retMsg": "Set leverage not modified"})

        if path == "/v5/order/create":
            body = json.loads(request.content.decode("utf-8"))
            # Verify SHORT Hard Stop-Loss uses triggerDirection=1 (rises to trigger) + MarkPrice + reduceOnly
            if "triggerPrice" in body:
                assert body["triggerBy"] == "MarkPrice"
                assert body["reduceOnly"] is True
                assert body["triggerDirection"] == 1  # BUY stop protecting SHORT position
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": "bybit-ord-777",
                        "orderLinkId": body["orderLinkId"],
                    },
                },
            )

        if path == "/v5/order/amend":
            amend_called = True
            body = json.loads(request.content.decode("utf-8"))
            assert body["orderId"] == "bybit-ord-777"
            assert body["triggerPrice"] == "3180.05"
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": "bybit-ord-777",
                        "orderLinkId": "T_SL_ETH_001",
                    },
                },
            )

        return httpx.Response(404, json={"retCode": 10001, "retMsg": f"Unhandled {path}"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_bybit_handler))
    adapter = BybitAdapter(
        api_key="BYBIT_TEST_KEY",
        api_secret="BYBIT_TEST_SECRET",
        is_testnet=True,
        max_leverage_cap=10,
        http_client=client,
    )

    # 1. Load markets & check ETH/USDT rules
    rules = await adapter.get_market_rules("ETH/USDT", MarketType.FUTURES_USDT)
    assert rules.tick_size == Decimal("0.05")
    assert rules.step_size == Decimal("0.01")
    assert rules.max_leverage == 10  # Capped from 50 to max_leverage_cap=10

    # 2. Fetch Unified Account Balance
    bal = await adapter.fetch_balance(MarketType.FUTURES_USDT)
    assert bal.total_equity == Decimal("10000.00")
    assert bal.available_balance == Decimal("8500.00")
    assert bal.unrealized_pnl == Decimal("42.50")

    # 3. Fetch OHLCV and verify ascending chronological sort
    bars = await adapter.fetch_ohlcv("ETH/USDT", MarketType.FUTURES_USDT, "15m", limit=2)
    assert len(bars) == 2
    assert bars[0].open_time < bars[1].open_time
    assert bars[0].close == Decimal("3110.0")
    assert bars[1].close == Decimal("3120.0")

    # 4. Configure Futures Symbol (One-Way Mode + Isolated Margin + 5x Leverage)
    await adapter.configure_futures_symbol(
        symbol="ETH/USDT",
        leverage=5,
        margin_mode=MarginMode.ISOLATED,
        one_way_mode=True,
    )

    # 5. Place Hard Stop-Loss for a SHORT position (side=BUY -> triggerDirection=1)
    sl_resp = await adapter.place_hard_stop_loss(
        symbol="ETH/USDT",
        market_type=MarketType.FUTURES_USDT,
        side=OrderSide.BUY,
        quantity=Decimal("1.509"),  # Quantized down to 1.50
        stop_price=Decimal("3200.04"),  # Quantized to 3200.05 (tick_size=0.05)
        client_order_id="T_SL_ETH_001",
    )
    assert sl_resp.exchange_order_id == "bybit-ord-777"
    assert sl_resp.requested_qty == Decimal("1.50")
    assert sl_resp.stop_price == Decimal("3200.05")

    # 6. Atomically amend Stop-Loss via Bybit's /v5/order/amend endpoint
    amended = await adapter.amend_or_replace_stop_loss(
        existing_order_id="bybit-ord-777",
        existing_client_order_id="T_SL_ETH_001",
        symbol="ETH/USDT",
        market_type=MarketType.FUTURES_USDT,
        side=OrderSide.BUY,
        quantity=Decimal("1.50"),
        new_stop_price=Decimal("3180.04"),  # Quantized to 3180.05
        new_client_order_id="T_SL_ETH_002",
    )
    assert amend_called is True
    assert amended.exchange_order_id == "bybit-ord-777"
    assert amended.stop_price == Decimal("3180.05")

    await client.aclose()


# =====================================================================
# 4. ExchangeAdapterFactory & CredentialVault Integration Test
# =====================================================================


def test_exchange_adapter_factory_decrypts_and_instantiates_adapters() -> None:
    vault = CredentialVault("factory-master-secret-key-32-bytes!!")
    factory = ExchangeAdapterFactory(vault=vault)

    binance_conn = ExchangeConnection(
        exchange_id=ExchangeId.BINANCE.value,
        label="Binance Testnet",
        api_key_masked=mask_api_key("BINANCE_KEY_123456"),
        api_key_encrypted=vault.encrypt("BINANCE_KEY_123456", associated_data="BINANCE"),
        api_secret_encrypted=vault.encrypt("BINANCE_SECRET_654321", associated_data="BINANCE"),
        is_testnet=True,
    )
    bybit_conn = ExchangeConnection(
        exchange_id=ExchangeId.BYBIT.value,
        label="Bybit Live",
        api_key_masked=mask_api_key("BYBIT_KEY_ABCDEF"),
        api_key_encrypted=vault.encrypt("BYBIT_KEY_ABCDEF", associated_data="BYBIT"),
        api_secret_encrypted=vault.encrypt("BYBIT_SECRET_FEDCBA", associated_data="BYBIT"),
        is_testnet=False,
    )

    binance_adapter = factory.create_from_connection(binance_conn)
    bybit_adapter = factory.create_from_connection(bybit_conn)

    assert isinstance(binance_adapter, BinanceAdapter)
    assert binance_adapter.exchange_id == ExchangeId.BINANCE
    assert binance_adapter.is_testnet is True

    assert isinstance(bybit_adapter, BybitAdapter)
    assert bybit_adapter.exchange_id == ExchangeId.BYBIT
    assert bybit_adapter.is_testnet is False
