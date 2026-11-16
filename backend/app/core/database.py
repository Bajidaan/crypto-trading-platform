"""SQLAlchemy 2.0 Async Database Engine, WAL configuration, and Session management."""

from collections.abc import AsyncGenerator
from functools import partial
import os
from pathlib import Path
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
    """Make aiosqlite run SQLite calls directly on the event loop instead of its worker thread.

    ONLY for sandboxes where the event loop's loopback self-pipe is blocked, so aiosqlite's
    thread can never wake the loop (the test suite's conftest is one). Never enable it for
    the trading engine: every query and commit then freezes the whole process, including
    the Binance kline stream and order placement (measured 3 ms median, 13 ms p99 per
    write). Enabled only when SQLITE_INLINE_MODE=1.
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


if os.environ.get("SQLITE_INLINE_MODE", "").strip().lower() in ("1", "true", "yes"):
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


MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
BASELINE_REVISION = "0001"


def _upgrade_schema(sync_conn: Any) -> None:
    """Apply Alembic migrations on an open connection.

    A database created before migrations existed (tables present, no alembic_version
    table) matches the baseline revision, so it is stamped at the baseline first and
    then upgraded like any other.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["connection"] = sync_conn

    tables = set(inspect(sync_conn).get_table_names())
    if "alembic_version" not in tables and "positions" in tables:
        command.stamp(cfg, BASELINE_REVISION)
    command.upgrade(cfg, "head")


async def init_database(engine: AsyncEngine) -> None:
    """Bring the schema up to date with migrations and seed the singleton GlobalRiskState row."""
    from sqlalchemy import select
    from app.models.entities import GlobalRiskState

    async with engine.connect() as conn:
        is_sqlite = conn.dialect.name == "sqlite"
        if is_sqlite:
            # SQLite migrations rebuild tables (create copy, drop original, rename). With
            # foreign keys on, dropping `positions` would cascade-delete its orders. The
            # pragma is ignored inside a transaction, so it is set before one starts and
            # restored on this same connection afterwards.
            await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            await conn.commit()
        try:
            await conn.run_sync(_upgrade_schema)
            await conn.commit()
        finally:
            if is_sqlite:
                await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                await conn.commit()

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
