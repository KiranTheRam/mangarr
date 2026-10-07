from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import config

# how long a writer waits for another connection's write lock before failing
# with "database is locked" (the sqlite3 driver's own default is 5s)
BUSY_TIMEOUT_MS = 30_000

engine = create_async_engine(config.db_url, echo=False)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    # WAL lets API reads and job reads proceed while a job writes, so only
    # writer-vs-writer contention is left for busy_timeout to absorb. The mode
    # is persistent in the file; an in-memory database answers "memory" and
    # stays as it is. busy_timeout goes first so the one-time switch to WAL
    # also waits out a lock instead of failing.
    cursor = dbapi_connection.cursor()
    cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    # MANGARR_SQLITE_WAL=false is the opt-out for network storage; it also
    # switches a database that is already in WAL back to the rollback journal.
    cursor.execute(f"PRAGMA journal_mode={'WAL' if config.sqlite_wal else 'DELETE'}")
    cursor.close()


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
    ("series", "folder_pinned", "BOOLEAN NOT NULL DEFAULT 0", None),
    ("series", "metadata_refreshed_at", "DATETIME", None),
    ("chapters", "title_source", "VARCHAR NOT NULL DEFAULT ''", None),
    ("chapters", "volume_source", "VARCHAR NOT NULL DEFAULT ''", None),
    ("chapters", "title_locked", "BOOLEAN NOT NULL DEFAULT 0", None),
    ("chapters", "volume_locked", "BOOLEAN NOT NULL DEFAULT 0", None),
    ("chapters", "excluded", "BOOLEAN NOT NULL DEFAULT 0", None),
    # NULL means the chapter has not had source availability evaluated yet;
    # an empty string means a successful refresh found no direct source.
    ("chapters", "available_sources", "VARCHAR", None),
    ("series", "monitor_mode", "VARCHAR NOT NULL DEFAULT 'all'", None),
    ("series", "monitor_from", "FLOAT", None),
    ("series", "source_priority", "TEXT NOT NULL DEFAULT ''", None),
    ("series", "blocked_sources", "TEXT NOT NULL DEFAULT ''", None),
    ("series", "preferred_groups", "TEXT NOT NULL DEFAULT ''", None),
    ("series", "blocked_groups", "TEXT NOT NULL DEFAULT ''", None),
    ("series", "upgrades_enabled", "BOOLEAN NOT NULL DEFAULT 0", None),
    ("series", "upgrade_cutoff", "VARCHAR NOT NULL DEFAULT ''", None),
    ("series", "merge_volumes", "BOOLEAN NOT NULL DEFAULT 0", None),
    ("series", "last_monitored_at", "DATETIME", None),
    ("chapters", "file_source", "VARCHAR NOT NULL DEFAULT ''", None),
    ("chapters", "file_group", "VARCHAR NOT NULL DEFAULT ''", None),
    ("downloads", "release_group", "VARCHAR NOT NULL DEFAULT ''", None),
    ("series_folders", "volume_offset", "INTEGER", None),
]

# Chapters mangarr downloaded before file provenance was tracked: the latest
# "imported" history event whose path is still the chapter's file names the
# source. Runs once, when the file_source column is first added.
_BACKFILL_FILE_SOURCE = """
UPDATE chapters SET file_source = COALESCE((
    SELECT h.source_name FROM history h
    WHERE h.chapter_id = chapters.id AND h.event = 'imported'
      AND h.detail = chapters.file_path
    ORDER BY h.id DESC LIMIT 1
), '')
WHERE downloaded = 1 AND file_path != ''
"""


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
                if (table, column) == ("chapters", "file_source"):
                    await conn.exec_driver_sql(_BACKFILL_FILE_SOURCE)
            if index:
                await conn.exec_driver_sql(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {index} ON {table} ({column})"
                )
