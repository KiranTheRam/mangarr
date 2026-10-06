"""SQLite lock contention (#28): the app's database must run in WAL mode with
a generous busy timeout, and the direct-download worker must not hold a write
transaction open between pages — otherwise every other writer (qBittorrent
sync, monitor, settings saves) fails with "database is locked"."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import mangarr
from mangarr.jobs import tasks
from mangarr.models import (
    Base,
    Chapter,
    Download,
    DownloadKind,
    DownloadStatus,
    HistoryEvent,
    RootFolder,
    Series,
)
from mangarr.sources.base import DirectSource

# the app engine is built from process config at import time, so it is
# inspected in a fresh interpreter pointed at a throwaway data dir
_PROBE = """
import asyncio, json, sqlite3
from mangarr import db
from mangarr.config import config

async def main():
    await db.init_db()
    async with db.engine.connect() as conn:
        out = {p: (await conn.exec_driver_sql(f"PRAGMA {p}")).scalar()
               for p in ("journal_mode", "busy_timeout")}
    await db.engine.dispose()
    # WAL is a property of the file, so a plain connection sees it too
    raw = sqlite3.connect(config.db_path)
    out["file_journal_mode"] = raw.execute("PRAGMA journal_mode").fetchone()[0]
    raw.close()
    print(json.dumps(out))

asyncio.run(main())
"""


def test_app_engine_uses_wal_and_a_long_busy_timeout(tmp_path):
    env = dict(os.environ)
    env["MANGARR_DATA_DIR"] = str(tmp_path / "data")
    env["PYTHONPATH"] = str(Path(mangarr.__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    pragmas = json.loads(result.stdout.strip().splitlines()[-1])

    assert pragmas["journal_mode"] == "wal"
    assert pragmas["file_journal_mode"] == "wal"
    # longer than the sqlite3 driver's own 5s default, which the audit's
    # repro showed is not enough
    assert pragmas["busy_timeout"] > 5000


class FakeSource(DirectSource):
    name = "fake"

    async def search_series(self, query):
        return []

    async def list_chapters(self, external_id):
        return []

    async def get_pages(self, chapter_external_id):
        return []


async def test_progress_between_commits_does_not_hold_the_write_lock(
    tmp_path, monkeypatch
):
    """Two pages land within the 1s progress throttle, then the third is slow.
    While it is in flight, another connection must be able to write."""
    from mangarr import settings_service
    monkeypatch.setitem(settings_service.DEFAULTS, "source_fake_enabled", "true")
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())

    url = f"sqlite+aiosqlite:///{tmp_path / 'locking.db'}"
    worker_engine = create_async_engine(url)
    # a short timeout so a held lock fails the test fast instead of waiting
    other_engine = create_async_engine(url, connect_args={"timeout": 0.2})
    async with worker_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    mid_flight = asyncio.Event()
    release = asyncio.Event()

    async def slow_last_page(source, payload, series, chapter, dest,
                             progress_cb=None, cancel_cb=None, web_url=""):
        # the real fetch loop's per-page sequence: cancel check, progress,
        # cancel check — the last check is the SELECT that would autoflush
        await cancel_cb()
        for done in (1, 2):
            await cancel_cb()
            await progress_cb(done, 3)
            await cancel_cb()
        mid_flight.set()
        await release.wait()
        await cancel_cb()
        await progress_cb(3, 3)
        await cancel_cb()

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", slow_last_page)

    maker = async_sessionmaker(worker_engine, expire_on_commit=False)
    other_maker = async_sessionmaker(other_engine, expire_on_commit=False)
    try:
        async with maker() as session:
            series = Series(
                title="Test Series", sort_title="test series",
                root_folder=RootFolder(path=str(tmp_path)), folder_name="Test Series",
            )
            chapter = Chapter(number=1.0, monitored=True)
            series.chapters.append(chapter)
            session.add(series)
            await session.commit()
            dl = Download(
                series_id=series.id, chapter_id=chapter.id,
                kind=DownloadKind.DIRECT, status=DownloadStatus.QUEUED,
                source_name="fake", payload="c1", title="Test Series - Chapter 1",
            )
            session.add(dl)
            await session.commit()

            worker = asyncio.create_task(tasks._run_direct_download(session, dl))
            try:
                await asyncio.wait_for(mid_flight.wait(), timeout=5)
                async with other_maker() as other:
                    other.add(HistoryEvent(event="test", source_name="other",
                                           detail="concurrent writer"))
                    try:
                        await other.commit()
                    except OperationalError as exc:
                        pytest.fail(f"concurrent writer blocked mid-download: {exc.orig}")
            finally:
                release.set()
                await asyncio.wait_for(worker, timeout=5)

            await session.refresh(dl)
            assert dl.status == DownloadStatus.DONE
            assert dl.progress == 1.0
    finally:
        await worker_engine.dispose()
        await other_engine.dispose()
