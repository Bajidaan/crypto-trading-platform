"""FastAPI Dependency Injection and Application State Singletons."""

from collections.abc import AsyncGenerator
from functools import lru_cache
import logging
from typing import Any
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.database import build_async_engine, build_session_factory
from app.core.events import EventBus
from app.core.security import CredentialVault
from app.domains.cex.adapters.factory import ExchangeAdapterFactory
from app.domains.cex.execution.paper_broker import PaperTradingBroker
from app.domains.cex.execution.runner import ContinuousStrategyRunner
from app.domains.cex.execution.sl_tp_manager import HybridSlTpManager
from app.domains.cex.market_data.stream_manager import MarketStreamManager
from app.domains.cex.risk.gatekeeper import RiskGatekeeper

logger = logging.getLogger(__name__)


class AppState:
    """Singleton application state holding shared services across the lifetime of the process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.vault = CredentialVault(master_secret=settings.master_vault_secret)
        self.event_bus = EventBus()
        self.engine = build_async_engine(settings.database_url)
        self.session_factory = build_session_factory(self.engine)
        self.adapter_factory = ExchangeAdapterFactory(vault=self.vault, settings=settings)

        self.risk_gatekeeper = RiskGatekeeper(event_bus=self.event_bus)
        self.sl_tp_manager = HybridSlTpManager(event_bus=self.event_bus)
        self.paper_broker = PaperTradingBroker(event_bus=self.event_bus)
        self.stream_manager = MarketStreamManager(event_bus=self.event_bus)
        self.strategy_runner = ContinuousStrategyRunner(
            session_factory=self.session_factory,
            event_bus=self.event_bus,
            paper_broker=self.paper_broker,
            risk_gatekeeper=self.risk_gatekeeper,
            adapter_factory=self.adapter_factory,
            poll_interval_seconds=15.0,
        )


_app_state: AppState | None = None


def get_app_state() -> AppState:
    global _app_state
    if _app_state is None:
        _app_state = AppState(get_settings())
    return _app_state


def set_app_state(state: AppState) -> None:
    global _app_state
    _app_state = state


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for injecting an async database session per request."""
    state = get_app_state()
    async with state.session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_event_bus() -> EventBus:
    return get_app_state().event_bus


async def get_vault() -> CredentialVault:
    return get_app_state().vault


async def get_risk_gatekeeper() -> RiskGatekeeper:
    return get_app_state().risk_gatekeeper


async def get_sl_tp_manager() -> HybridSlTpManager:
    return get_app_state().sl_tp_manager


async def get_paper_broker() -> PaperTradingBroker:
    return get_app_state().paper_broker


async def get_stream_manager() -> MarketStreamManager:
    return get_app_state().stream_manager


async def get_adapter_factory() -> ExchangeAdapterFactory:
    return get_app_state().adapter_factory
