"""Seed initial built-in strategy configurations and sample paper positions into local DB."""

import asyncio
from decimal import Decimal
import uuid
from sqlalchemy import select

from app.config import get_settings
from app.core.database import build_async_engine, build_session_factory, init_database
from app.core.decimal_utils import generate_client_order_id
from app.models.entities import Order, Position, StrategyConfig
from app.models.enums import MarketType


async def seed() -> None:
    settings = get_settings()
    engine = build_async_engine(settings.database_url)
    await init_database(engine)

    session_factory = build_session_factory(engine)
    async with session_factory() as session:
        existing_strat = (await session.execute(select(StrategyConfig))).scalars().first()
        if existing_strat:
            print("Database already contains strategies. Skipping seed.")
            await engine.dispose()
            return

        print("Seeding initial strategies and sample paper trading data...")
        strat1 = StrategyConfig(
            id=str(uuid.uuid4()),
            name="BTC/ETH EMA RSI Trend",
            strategy_class="ema_rsi_atr",
            execution_mode="PAPER",
            market_type=MarketType.FUTURES_USDT.value,
            symbols=["BTC/USDT", "ETH/USDT"],
            timeframe="15m",
            parameters={
                "fast_ema_period": 9,
                "slow_ema_period": 21,
                "rsi_period": 14,
                "rsi_overbought": 70,
                "rsi_oversold": 30,
            },
            risk_config={"leverage": 3, "margin_mode": "ISOLATED", "risk_per_trade_pct": 1.0},
            is_active=True,
        )

        strat2 = StrategyConfig(
            id=str(uuid.uuid4()),
            name="SOL Volatility Breakout",
            strategy_class="bollinger_breakout",
            execution_mode="PAPER",
            market_type=MarketType.FUTURES_USDT.value,
            symbols=["SOL/USDT"],
            timeframe="1h",
            parameters={
                "bb_period": 20,
                "bb_std_dev": 2.0,
                "adx_period": 14,
                "adx_threshold": 25.0,
            },
            risk_config={"leverage": 3, "margin_mode": "ISOLATED", "risk_per_trade_pct": 1.0},
            is_active=True,
        )

        strat3 = StrategyConfig(
            id=str(uuid.uuid4()),
            name="Smart DCA Scale-In",
            strategy_class="smart_dca",
            execution_mode="PAPER",
            market_type=MarketType.FUTURES_USDT.value,
            symbols=["BTC/USDT"],
            timeframe="15m",
            parameters={
                "base_order_size_usd": 150.0,
                "base_entry_rsi_threshold": 35.0,
                "rsi_period": 14,
                "safety_order_step_pct": 2.5,
                "max_safety_orders": 3,
                "take_profit_pct": 2.0,
            },
            risk_config={"leverage": 2, "margin_mode": "ISOLATED", "risk_per_trade_pct": 1.0},
            is_active=False,
        )

        session.add_all([strat1, strat2, strat3])
        await session.commit()

        # Seed sample paper position
        pos1 = Position(
            id=str(uuid.uuid4()),
            strategy_id=strat1.id,
            execution_mode="PAPER",
            symbol="BTC/USDT",
            market_type=MarketType.FUTURES_USDT.value,
            side="LONG",
            margin_mode="ISOLATED",
            leverage=3,
            entry_price=Decimal("65200.00"),
            initial_qty=Decimal("0.05"),
            current_qty=Decimal("0.05"),
            stop_loss_price=Decimal("63896.00"),
            stop_order_live=True,
            status="OPEN",
        )
        session.add(pos1)
        await session.commit()

        # Seed sample fill order
        order1 = Order(
            id=str(uuid.uuid4()),
            position_id=pos1.id,
            client_order_id=generate_client_order_id("ENTRY", strat1.id, "BTC/USDT", "sig-init-01"),
            symbol="BTC/USDT",
            market_type=MarketType.FUTURES_USDT.value,
            purpose="ENTRY",
            order_type="MARKET",
            side="BUY",
            requested_qty=Decimal("0.05"),
            filled_qty=Decimal("0.05"),
            avg_fill_price=Decimal("65200.00"),
            fee_usd=Decimal("1.95"),
            status="FILLED",
        )
        session.add(order1)
        await session.commit()
        print("Seed completed successfully.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
