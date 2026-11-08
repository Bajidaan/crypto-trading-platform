"""SQLAlchemy 2.0 Async Database Engine, WAL configuration, and Session management."""

from collections.abc import AsyncGenerator
from functools import partial
from typing import Any
import aiosqlite.core
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


def _configure_aiosqlite_inline_mode() -> None:
    """Configure aiosqlite Connection to execute in-process SQLite operations inline.

    On Windows environments with strict loopback socket sandboxing, aiosqlite's
    background worker thread hangs on `loop.call_soon_threadsafe()` (which uses
    a TCP loopback socketpair). Running local embedded SQLite operations inline
    eliminates cross-thread loopback wakeups and thread-switching overhead.
    """
    if getattr(aiosqlite.core.Connection, "_inline_patched", False):
        return

    async def _inline_connect(self: aiosqlite.core.Connection) -> aiosqlite.core.Connection:
        if self._connection is None:
            self._connection = self._connector()
        return self

    def _inline_await(self: aiosqlite.core.Connection) -> Any:
        return self._connect().__await__()

    async def _inline_execute(self: aiosqlite.core.Connection, fn: Any, *args: Any, **kwargs: Any) -> Any:
        if not self._running or not self._connection:
            raise ValueError("Connection closed")
        return partial(fn, *args, **kwargs)()

    def _inline_stop(self: aiosqlite.core.Connection) -> None:
        self._running = False
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        return None

    async def _inline_close(self: aiosqlite.core.Connection) -> None:
        self.stop()

    from sqlalchemy.dialects.sqlite.aiosqlite import AsyncAdapt_aiosqlite_connection

    def _get_isolation_level(self: AsyncAdapt_aiosqlite_connection) -> str | None:
        return self._connection.isolation_level

    def _set_isolation_level(self: AsyncAdapt_aiosqlite_connection, value: str | None) -> None:
        try:
            self._connection._conn.isolation_level = value
        except Exception as error:
            self._handle_exception(error)

    aiosqlite.core.Connection._connect = _inline_connect  # type: ignore[method-assign]
    aiosqlite.core.Connection.__await__ = _inline_await  # type: ignore[method-assign]
    aiosqlite.core.Connection._execute = _inline_execute  # type: ignore[method-assign]
    aiosqlite.core.Connection.stop = _inline_stop  # type: ignore[method-assign]
    aiosqlite.core.Connection.close = _inline_close  # type: ignore[method-assign]
    AsyncAdapt_aiosqlite_connection.isolation_level = property(  # type: ignore[assignment]
        _get_isolation_level, _set_isolation_level
    )
    aiosqlite.core.Connection._inline_patched = True  # type: ignore[attr-defined]


_configure_aiosqlite_inline_mode()


class Base(DeclarativeBase):
    """Declarative base class for all SQLAlchemy ORM entities."""


def _configure_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Enable WAL mode, foreign keys, and busy timeout on SQLite connections."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.execute("PRAGMA busy_timeout=5000;")
    cursor.close()


def build_async_engine(database_url: str | None = None, echo: bool = False) -> AsyncEngine:
    """Create an AsyncEngine configured for either SQLite (with WAL) or PostgreSQL."""
    url = database_url or get_settings().database_url
    engine = create_async_engine(url, echo=echo, future=True)
    if url.startswith("sqlite"):
        event.listen(engine.sync_engine, "connect", _configure_sqlite_pragmas)
    return engine


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async_sessionmaker bound to the given engine."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def init_database(engine: AsyncEngine) -> None:
    """Create all database tables and seed the singleton GlobalRiskState row if absent."""
    from sqlalchemy import select
    from app.models.entities import GlobalRiskState

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = build_session_factory(engine)
    async with session_factory() as session:
        result = await session.execute(select(GlobalRiskState).where(GlobalRiskState.id == 1))
        existing = result.scalar_one_or_none()
        if existing is None:
            settings = get_settings()
            default_risk = GlobalRiskState(
                id=1,
                is_paused=False,
                kill_switch_active=False,
                max_concurrent_positions=settings.default_max_concurrent_positions,
                max_daily_drawdown_pct=settings.default_max_daily_drawdown_pct,
                max_leverage_cap=settings.default_max_leverage_cap,
                default_risk_per_trade_pct=settings.default_risk_per_trade_pct,
                compounding_enabled=False,
            )
            session.add(default_risk)
            await session.commit()


async def get_db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession for dependency injection."""
    async with session_factory() as session:
        yield session
