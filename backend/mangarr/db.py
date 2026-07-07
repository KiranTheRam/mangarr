from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import config

engine = create_async_engine(config.db_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """For background jobs that need their own session."""
    async with SessionLocal() as session:
        yield session


# columns added after a table already shipped — create_all won't alter
# existing tables, so they're added here (SQLite ALTER TABLE ADD COLUMN)
_COLUMN_MIGRATIONS: list[tuple[str, str, str, str | None]] = [
    # (table, column, type, unique index name or None)
    ("series", "mangaupdates_id", "BIGINT", "ux_series_mangaupdates_id"),
]


async def init_db() -> None:
    from . import models  # noqa: F401 — register mappings

    config.data_dir.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
        for table, column, col_type, index in _COLUMN_MIGRATIONS:
            info = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
            if column not in {row[1] for row in info.fetchall()}:
                await conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                )
            if index:
                await conn.exec_driver_sql(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {index} ON {table} ({column})"
                )
