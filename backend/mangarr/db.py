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


async def init_db() -> None:
    from . import models  # noqa: F401 — register mappings

    config.data_dir.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
