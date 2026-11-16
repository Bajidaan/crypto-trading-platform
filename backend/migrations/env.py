"""Alembic environment.

Two ways in:
- `alembic upgrade head` from the backend folder: connects using DATABASE_URL from settings.
- `init_database()` at app startup: passes its own open connection in
  `config.attributes["connection"]`, so migrations run inside the app's engine.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.core.database import Base, build_async_engine
import app.models.entities  # noqa: F401  (registers tables on Base.metadata)

config = context.config
if config.config_file_name is not None and not config.attributes.get("connection"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _configure_and_run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most column properties; batch mode rebuilds the table instead.
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = build_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        await conn.run_sync(_configure_and_run)
        await conn.commit()
    await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure_and_run(connection)
    else:
        asyncio.run(_run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
