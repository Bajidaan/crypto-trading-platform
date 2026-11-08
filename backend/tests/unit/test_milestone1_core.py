"""Milestone 1 Test Suite: Decimal Math, Security Vault, Event Bus, and Database ORM."""

from decimal import Decimal
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.audit import AuditLogger
from app.core.database import build_async_engine, build_session_factory, init_database
from app.core.decimal_utils import (
    CLIENT_ORDER_ID_PATTERN,
    calculate_position_pnl,
    generate_client_order_id,
    quantize_price,
    quantize_quantity,
    to_decimal,
    validate_order_filters,
)
from app.core.events import (
    DomainEvent,
    EventBus,
    MarketCandleEvent,
    OrderLifecycleEvent,
    PositionUpdatedEvent,
    RiskAlertEvent,
    TradeSignalEvent,
)
from app.core.notifications import NotificationDispatcher
from app.core.security import (
    CredentialVault,
    VaultDecryptionError,
    WebhookAuthenticationError,
    compute_webhook_signature,
    mask_api_key,
    verify_webhook_signature,
)
from app.models.entities import (
    ExchangeConnection,
    Fill,
    GlobalRiskState,
    Order,
    Position,
    StrategyConfig,
    TradeSignalRecord,
)
from app.models.enums import (
    ExchangeId,
    ExecutionMode,
    MarginMode,
    MarketType,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    SignalSource,
    SignalStatus,
)


# =====================================================================
# 1. Fixed-Point Decimal & Exchange Quantizer Tests
# =====================================================================


def test_to_decimal_avoids_float_precision_artifacts() -> None:
    d1 = to_decimal(0.1)
    d2 = to_decimal(0.2)
    assert d1 + d2 == Decimal("0.3")


def test_quantize_price_standard_and_custom_tick_sizes() -> None:
    # Standard 0.01 tick size
    assert quantize_price("64231.456", "0.01") == Decimal("64231.46")
    # Non-power-of-10 tick size (0.05)
    assert quantize_price("12.33", "0.05") == Decimal("12.35")
    assert quantize_price("12.31", "0.05") == Decimal("12.30")
    # Small altcoin tick size (0.00001)
    assert quantize_price("0.0456789", "0.00001") == Decimal("0.04568")


def test_quantize_quantity_always_rounds_down() -> None:
    # Even at .999 of a step, quantity MUST round down to never exceed balance/margin
    assert quantize_quantity("1.2399", "0.01") == Decimal("1.23")
    assert quantize_quantity("0.00599", "0.001") == Decimal("0.005")
    assert quantize_quantity("149.9", "10") == Decimal("140")


def test_validate_order_filters() -> None:
    # Valid order
    ok, err = validate_order_filters(
        price=Decimal("50000"),
        quantity=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("10"),
    )
    assert ok is True
    assert err is None

    # Below min_qty
    ok, err = validate_order_filters(
        price=Decimal("50000"),
        quantity=Decimal("0.0005"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("10"),
    )
    assert ok is False
    assert "minimum quantity" in (err or "")

    # Below min_notional
    ok, err = validate_order_filters(
        price=Decimal("100"),
        quantity=Decimal("0.05"),  # notional = $5 < $10
        min_qty=Decimal("0.01"),
        min_notional=Decimal("10"),
    )
    assert ok is False
    assert "minimum notional" in (err or "")


def test_calculate_position_pnl_long_and_short() -> None:
    # LONG position: entry 100, exit 115, qty 2, fee $1.50 -> gross = +30, net = +28.50
    gross_long, net_long = calculate_position_pnl("LONG", "100", "115", "2", "1.50")
    assert gross_long == Decimal("30")
    assert net_long == Decimal("28.50")

    # SHORT position: entry 100, exit 85, qty 2, fee $1.50 -> gross = +30, net = +28.50
    gross_short, net_short = calculate_position_pnl("SHORT", "100", "85", "2", "1.50")
    assert gross_short == Decimal("30")
    assert net_short == Decimal("28.50")

    # SHORT losing position: entry 100, exit 110, qty 2, fee $1 -> gross = -20, net = -21
    gross_loss, net_loss = calculate_position_pnl("SHORT", "100", "110", "2", "1.0")
    assert gross_loss == Decimal("-20")
    assert net_loss == Decimal("-21.0")


def test_generate_client_order_id_determinism_and_constraints() -> None:
    cid_1 = generate_client_order_id("ENTRY", "strat-123", "BTC/USDT", "bar-1700000000")
    cid_2 = generate_client_order_id("ENTRY", "strat-123", "BTC/USDT", "bar-1700000000")
    cid_diff = generate_client_order_id("HARD_STOP_LOSS", "strat-123", "BTC/USDT", "bar-1700000000")

    assert cid_1 == cid_2  # Deterministic across retries
    assert cid_1 != cid_diff
    assert len(cid_1) <= 36
    assert CLIENT_ORDER_ID_PATTERN.match(cid_1) is not None


# =====================================================================
# 2. Security Vault & Webhook HMAC Tests
# =====================================================================


def test_credential_vault_encrypt_decrypt_and_tamper_detection() -> None:
    vault = CredentialVault("super-secret-master-key-for-testing-12345")
    raw_secret = "bybit_live_secret_987654321_abcdef"

    token1 = vault.encrypt(raw_secret, associated_data="BYBIT")
    token2 = vault.encrypt(raw_secret, associated_data="BYBIT")
    assert token1 != token2  # Random salt + nonce per encryption

    assert vault.decrypt(token1, associated_data="BYBIT") == raw_secret
    assert vault.decrypt(token2, associated_data="BYBIT") == raw_secret

    # Wrong Associated Data must fail authentication
    with pytest.raises(VaultDecryptionError):
        vault.decrypt(token1, associated_data="BINANCE")

    # Wrong Master Secret must fail authentication
    wrong_vault = CredentialVault("different-master-key-for-testing-999999")
    with pytest.raises(VaultDecryptionError):
        wrong_vault.decrypt(token1, associated_data="BYBIT")

    # Tampered ciphertext must fail GCM tag verification
    tampered = token1[:-4] + ("AAAA" if token1[-4:] != "AAAA" else "BBBB")
    with pytest.raises(VaultDecryptionError):
        vault.decrypt(tampered, associated_data="BYBIT")


def test_mask_api_key() -> None:
    assert mask_api_key("ABCDEFGHIJKLMNOP") == "ABCD****MNOP"
    assert mask_api_key("short") == "****"


def test_webhook_hmac_verification_and_replay_protection() -> None:
    secret = "tradingview-webhook-secret-key-2026"
    body = b'{"symbol":"BTC/USDT","side":"LONG","stop_loss":62000}'
    ts = 1_750_000_000
    sig = compute_webhook_signature(secret, ts, body)

    # Valid signature within timestamp window
    assert verify_webhook_signature(secret, ts, body, sig, max_skew_seconds=300, now_ts=ts + 30) is True

    # Invalid signature rejected
    with pytest.raises(WebhookAuthenticationError, match="HMAC signature"):
        verify_webhook_signature(secret, ts, body, "0" * 64, max_skew_seconds=300, now_ts=ts + 10)

    # Replay attack (stale timestamp > 300s) rejected
    with pytest.raises(WebhookAuthenticationError, match="timestamp skew"):
        verify_webhook_signature(secret, ts, body, sig, max_skew_seconds=300, now_ts=ts + 600)


# =====================================================================
# 3. Asynchronous Event Bus Tests
# =====================================================================


async def test_event_bus_pubsub_and_error_isolation() -> None:
    bus = EventBus()
    received_topics: list[str] = []
    wildcard_events: list[DomainEvent] = []

    async def good_listener(event: DomainEvent) -> None:
        received_topics.append(event.topic)

    async def crashing_listener(_event: DomainEvent) -> None:
        raise RuntimeError("Simulated subscriber failure")

    async def wildcard_listener(event: DomainEvent) -> None:
        wildcard_events.append(event)

    bus.subscribe("risk:alert", crashing_listener)
    bus.subscribe("risk:alert", good_listener)
    bus.subscribe("*", wildcard_listener)

    alert = RiskAlertEvent(
        severity="CRITICAL",
        rule_name="MAX_DAILY_DRAWDOWN",
        message="Daily drawdown reached 5.0%, pausing new entries.",
    )
    await bus.publish(alert)

    # Good listener and wildcard listener still succeeded despite crashing_listener raising
    assert received_topics == ["risk:alert"]
    assert len(wildcard_events) == 1
    assert bus.stats["published_count"] == 1
    assert bus.stats["error_count"] == 1


# =====================================================================
# 4. Async Database Schema, Relationships, & Idempotency Tests
# =====================================================================


async def test_database_initialization_and_full_entity_lifecycle() -> None:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    session_factory = build_session_factory(engine)
    vault = CredentialVault("test-master-vault-secret-32-bytes!!")

    async with session_factory() as session:
        # 1. Verify singleton GlobalRiskState was seeded with our locked defaults
        risk_state = (
            await session.execute(select(GlobalRiskState).where(GlobalRiskState.id == 1))
        ).scalar_one()
        assert risk_state.is_paused is False
        assert risk_state.kill_switch_active is False
        assert risk_state.max_concurrent_positions == 5
        assert risk_state.max_daily_drawdown_pct == Decimal("5.0")
        assert risk_state.max_leverage_cap == 10

        # 2. Create an ExchangeConnection with AES-256-GCM encrypted credentials
        raw_key = "BYBIT_KEY_1234567890"
        raw_secret = "BYBIT_SECRET_ABCDEFGHIJKLMN"
        conn = ExchangeConnection(
            exchange_id=ExchangeId.BYBIT.value,
            label="Bybit Main Futures",
            api_key_masked=mask_api_key(raw_key),
            api_key_encrypted=vault.encrypt(raw_key, associated_data="BYBIT"),
            api_secret_encrypted=vault.encrypt(raw_secret, associated_data="BYBIT"),
            is_testnet=True,
            spot_capital_cap_usd=Decimal("2000"),
            futures_capital_cap_usd=Decimal("5000"),
        )
        session.add(conn)
        await session.flush()

        # 3. Create StrategyConfig -> Signal -> Position -> Order -> Fill
        strat = StrategyConfig(
            name="BTC 15m EMA RSI Trend",
            strategy_class="ema_rsi_atr",
            execution_mode=ExecutionMode.PAPER.value,
            exchange_connection_id=conn.id,
            market_type=MarketType.FUTURES_USDT.value,
            symbols=["BTC/USDT"],
            timeframe="15m",
            parameters={"fast_ema": 9, "slow_ema": 21, "rsi_period": 14},
            risk_config={"leverage": 3, "margin_mode": MarginMode.ISOLATED.value},
            is_active=True,
        )
        session.add(strat)
        await session.flush()

        sig = TradeSignalRecord(
            strategy_id=strat.id,
            source=SignalSource.STRATEGY.value,
            symbol="BTC/USDT",
            market_type=MarketType.FUTURES_USDT.value,
            side=PositionSide.LONG.value,
            signal_price=Decimal("65000.00"),
            stop_loss_price=Decimal("63700.00"),
            status=SignalStatus.APPROVED.value,
        )
        session.add(sig)
        await session.flush()

        pos = Position(
            strategy_id=strat.id,
            exchange_connection_id=conn.id,
            signal_id=sig.id,
            execution_mode=ExecutionMode.PAPER.value,
            symbol="BTC/USDT",
            market_type=MarketType.FUTURES_USDT.value,
            side=PositionSide.LONG.value,
            margin_mode=MarginMode.ISOLATED.value,
            leverage=3,
            entry_price=Decimal("65000.00"),
            initial_qty=Decimal("0.05"),
            current_qty=Decimal("0.05"),
            stop_loss_price=Decimal("63700.00"),
            stop_order_live=True,
            status=PositionStatus.OPEN.value,
        )
        session.add(pos)
        await session.flush()

        cid = generate_client_order_id("ENTRY", strat.id, "BTC/USDT", sig.id)
        order = Order(
            position_id=pos.id,
            client_order_id=cid,
            exchange_order_id="exch-ord-001",
            symbol="BTC/USDT",
            market_type=MarketType.FUTURES_USDT.value,
            purpose=OrderPurpose.ENTRY.value,
            order_type=OrderType.MARKET.value,
            side=OrderSide.BUY.value,
            requested_qty=Decimal("0.05"),
            filled_qty=Decimal("0.05"),
            avg_fill_price=Decimal("65000.00"),
            fee_usd=Decimal("1.95"),
            status=OrderStatus.FILLED.value,
        )
        session.add(order)
        await session.flush()

        fill = Fill(
            order_id=order.id,
            exchange_trade_id="trade-fill-001",
            fill_price=Decimal("65000.00"),
            fill_qty=Decimal("0.05"),
            fee_amount=Decimal("1.95"),
            fee_currency="USDT",
        )
        session.add(fill)
        await session.commit()

        # 4. Verify UniqueConstraint on client_order_id blocks duplicate order submission
        duplicate_order = Order(
            position_id=pos.id,
            client_order_id=cid,  # Identical client_order_id!
            symbol="BTC/USDT",
            market_type=MarketType.FUTURES_USDT.value,
            purpose=OrderPurpose.ENTRY.value,
            order_type=OrderType.MARKET.value,
            side=OrderSide.BUY.value,
            requested_qty=Decimal("0.05"),
        )
        session.add(duplicate_order)
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    # 5. Verify AuditLogger persists immutable audit records
    audit = AuditLogger(session_factory)
    entry = await audit.record(
        category="SECURITY",
        action="EXCHANGE_KEY_ADDED",
        details={"exchange": "BYBIT", "is_testnet": True},
        severity="INFO",
    )
    assert entry.id is not None
    assert entry.category == "SECURITY"
    assert entry.action == "EXCHANGE_KEY_ADDED"

    await engine.dispose()


# =====================================================================
# 5. Notification Dispatcher Tests
# =====================================================================


async def test_notification_dispatcher_subscribes_and_formats_alerts() -> None:
    bus = EventBus()
    dispatcher = NotificationDispatcher(event_bus=bus)

    await bus.publish(
        PositionUpdatedEvent(
            position_id="pos-001",
            strategy_id="strat-001",
            symbol="SOL/USDT",
            market_type=MarketType.FUTURES_USDT,
            execution_mode=ExecutionMode.PAPER,
            side=PositionSide.LONG,
            status=PositionStatus.OPEN,
            current_qty=Decimal("10"),
            entry_price=Decimal("150.00"),
            stop_loss_price=Decimal("145.00"),
            stop_order_live=True,
        )
    )
    await bus.publish(
        OrderLifecycleEvent(
            order_id="ord-001",
            position_id="pos-001",
            client_order_id="T_ENTRY_SOLUSDT_123",
            symbol="SOL/USDT",
            market_type=MarketType.FUTURES_USDT,
            purpose=OrderPurpose.HARD_STOP_LOSS,
            order_type=OrderType.STOP_MARKET,
            side=OrderSide.SELL,
            status=OrderStatus.REJECTED,
            requested_qty=Decimal("10"),
            filled_qty=Decimal("0"),
            reason="ReduceOnly Order Rejected",
        )
    )

    assert len(dispatcher.sent_messages) == 2
    assert dispatcher.sent_messages[0]["title"] == "Position Opened"
    assert "SOL/USDT" in dispatcher.sent_messages[0]["message"]
    assert dispatcher.sent_messages[1]["title"] == "Order Execution Failure"
    assert "ReduceOnly Order Rejected" in dispatcher.sent_messages[1]["message"]

