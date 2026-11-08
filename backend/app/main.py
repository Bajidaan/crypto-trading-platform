"""FastAPI Application Entry Point with Lifespan, CORS, and Router Mounting."""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import get_app_state
from app.api.routes_connections import router as connections_router
from app.api.routes_market import router as market_router
from app.api.routes_orders import router as orders_router
from app.api.routes_positions import router as positions_router
from app.api.routes_strategies import router as strategies_router
from app.api.routes_system import router as system_router
from app.api.routes_webhooks import router as webhooks_router
from app.api.ws import hub, ws_router
from app.config import get_settings
from app.core.database import init_database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and graceful shutdown events."""
    logger.info("Initializing Automated Crypto Trading Platform backend...")
    state = get_app_state()

    # 1. Initialize Database schema and seed singleton GlobalRiskState
    await init_database(state.engine)

    # 2. Wire event bus to WebSocket broadcast hub
    hub.setup_event_bus_listener(state.event_bus)

    # 3. Hydrate open paper positions from database
    loaded_count = await state.paper_broker.load_open_positions_from_db(state.session_factory)
    logger.info("Hydrated %d open paper positions from database into active broker.", loaded_count)

    # 4. Start continuous background strategy evaluation & live market runner
    state.strategy_runner.start()

    logger.info("Trading engine initialization complete. Ready for requests.")
    yield

    logger.info("Shutting down trading engine...")
    await state.strategy_runner.stop()
    await state.engine.dispose()
    logger.info("Engine shutdown complete.")


def create_app() -> FastAPI:
    """Create configured FastAPI application."""
    settings = get_settings()
    app = FastAPI(
        title="Automated Crypto Trading Platform (CEX Engine)",
        version="1.0.0",
        description="High-performance algorithmic crypto trading platform for Binance & Bybit.",
        lifespan=lifespan,
    )

    # CORS configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount REST routers under /api/v1
    api_prefix = "/api/v1"
    app.include_router(system_router, prefix=api_prefix)
    app.include_router(connections_router, prefix=api_prefix)
    app.include_router(strategies_router, prefix=api_prefix)
    app.include_router(positions_router, prefix=api_prefix)
    app.include_router(orders_router, prefix=api_prefix)
    app.include_router(market_router, prefix=api_prefix)
    app.include_router(webhooks_router, prefix=api_prefix)

    # Mount WebSocket router
    app.include_router(ws_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "name": "Automated Crypto Trading Platform API",
            "version": "1.0.0",
            "status": "ONLINE",
            "docs_url": "/docs",
        }

    return app


app = create_app()
