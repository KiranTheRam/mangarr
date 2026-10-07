"""How long a failed direct download keeps the monitor off that chapter on
that source: a transient failure (network, timeout, 429/5xx, stall) is
retried within hours, anything else after days — and a failure stays visible
in Activity for as long as it blocks."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.api.queue import get_queue
from mangarr.download import direct
from mangarr.jobs import tasks
from mangarr.models import (
    Base,
    Chapter,
    Download,
    DownloadKind,
    DownloadStatus,
    RootFolder,
    Series,
    SeriesSourceLink,
)
from mangarr.sources.base import DirectSource, SourceChapter


class FakeSource(DirectSource):
    name = "fake"

    def __init__(self, page_error=None):
        self.page_error = page_error

    async def search_series(self, query):
        return []

    async def list_chapters(self, external_id):
        return [SourceChapter(source_name=self.name, external_id="c1", number=1.0)]

    async def get_pages(self, chapter_external_id):
        return ["https://img.test/1.png"]

    async def download_page(self, client, url):
        raise self.page_error


@pytest.fixture
async def db_session(monkeypatch):
    from mangarr import settings_service
    monkeypatch.setitem(settings_service.DEFAULTS, "source_fake_enabled", "true")
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def source(monkeypatch):
    src = FakeSource(page_error=httpx.ConnectError("connection refused"))
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", src)
    monkeypatch.setattr(tasks.registry, "enabled_direct_sources", lambda values: [src])
    return src


async def _make_series(session, tmp_path):
    series = Series(
        title="Test Series", sort_title="test series",
        root_folder=RootFolder(path=str(tmp_path)), folder_name="Test Series",
    )
    series.source_links.append(SeriesSourceLink(source_name="fake", external_id="x"))
    series.chapters.append(Chapter(number=1.0, monitored=True))
    session.add(series)
    await session.commit()
    return series.id


async def _queue(session, series_id):
    series = await tasks._load_series(session, series_id)
    dl = Download(
        series_id=series_id, chapter_id=series.chapters[0].id,
        kind=DownloadKind.DIRECT, status=DownloadStatus.QUEUED,
        source_name="fake", payload="c1",
    )
    session.add(dl)
    await session.commit()
    return dl


async def _fail(session, dl, monkeypatch, exc):
    """Run the queued download `dl` through the worker, dying with `exc`."""
    async def failing_download(*args, **kwargs):
        raise exc

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", failing_download)
    await tasks._run_direct_download(session, dl)
    await session.refresh(dl)
    assert dl.status == DownloadStatus.FAILED
    return dl


async def _age(session, age):
    """Pretend every failure happened `age` ago."""
    for dl in (await session.execute(select(Download))).scalars():
        if dl.status == DownloadStatus.FAILED:
            dl.updated_at = datetime.now(timezone.utc) - age
    await session.commit()


async def _grab(session, series_id):
    series = await tasks._load_series(session, series_id)
    return await tasks.grab_missing_chapters(session, series, {})


def _http_error(status):
    request = httpx.Request("GET", "https://src.test/chapter")
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=httpx.Response(status, request=request)
    )


def _page_failure(cause):
    exc = RuntimeError(f"page 2 failed: {cause}")
    exc.__cause__ = cause
    return exc


TRANSIENT = {
    "connect-error": lambda: httpx.ConnectError("connection refused"),
    "read-timeout": lambda: httpx.ReadTimeout("timed out"),
    "http-503": lambda: _http_error(503),
    "http-429": lambda: _http_error(429),
    "wrapped-page-timeout": lambda: _page_failure(httpx.ConnectTimeout("timed out")),
}

PERMANENT = {
    "no-pages": lambda: RuntimeError("fake returned no pages for chapter 1"),
    "parse-error": lambda: ValueError("unexpected payload"),
    "http-404": lambda: _http_error(404),
    "wrapped-page-404": lambda: _page_failure(_http_error(404)),
}


@pytest.mark.parametrize("make_exc", TRANSIENT.values(), ids=TRANSIENT.keys())
async def test_transient_failure_is_retried_after_hours(
    db_session, tmp_path, monkeypatch, source, make_exc
):
    series_id = await _make_series(db_session, tmp_path)
    await _fail(db_session, await _queue(db_session, series_id), monkeypatch, make_exc())

    await _age(db_session, timedelta(minutes=30))
    assert await _grab(db_session, series_id) == 0  # still sits out a pass

    await _age(db_session, timedelta(hours=3))
    assert await _grab(db_session, series_id) == 1


@pytest.mark.parametrize("make_exc", PERMANENT.values(), ids=PERMANENT.keys())
async def test_permanent_failure_keeps_the_long_block(
    db_session, tmp_path, monkeypatch, source, make_exc
):
    series_id = await _make_series(db_session, tmp_path)
    await _fail(db_session, await _queue(db_session, series_id), monkeypatch, make_exc())

    await _age(db_session, timedelta(days=3))
    assert await _grab(db_session, series_id) == 0

    await _age(db_session, tasks.FAILED_GRAB_RETRY_AFTER + timedelta(days=1))
    assert await _grab(db_session, series_id) == 1


async def test_stalled_download_is_retried_after_hours(
    db_session, tmp_path, monkeypatch, source
):
    series_id = await _make_series(db_session, tmp_path)
    dl = await _queue(db_session, series_id)
    monkeypatch.setattr(tasks, "DIRECT_STALL_TIMEOUT", timedelta(seconds=0.2))

    async def hanging_download(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", hanging_download)
    await asyncio.wait_for(tasks._run_direct_download(db_session, dl), timeout=10)
    await db_session.refresh(dl)
    assert "stalled" in dl.error

    await _age(db_session, timedelta(hours=3))
    assert await _grab(db_session, series_id) == 1


async def test_page_network_error_from_the_real_downloader_is_transient(
    db_session, tmp_path, monkeypatch, source
):
    """The page worker wraps the error ("page 1 failed: ...") inside a task
    group; its network cause must survive the trip."""
    real_sleep = asyncio.sleep

    async def no_wait(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(direct.asyncio, "sleep", no_wait)  # page retry back-off
    series_id = await _make_series(db_session, tmp_path)
    dl = await _queue(db_session, series_id)

    await tasks._run_direct_download(db_session, dl)
    await db_session.refresh(dl)
    assert dl.status == DownloadStatus.FAILED
    assert "page 1 failed" in dl.error

    await _age(db_session, timedelta(hours=3))
    assert await _grab(db_session, series_id) == 1


async def test_repeated_transient_failures_get_the_long_block(
    db_session, tmp_path, monkeypatch, source
):
    monkeypatch.setattr(tasks, "TRANSIENT_GRAB_ATTEMPTS", 3, raising=False)
    series_id = await _make_series(db_session, tmp_path)
    for _ in range(2):
        await _fail(db_session, await _queue(db_session, series_id), monkeypatch,
                    httpx.ConnectError("connection refused"))
    await _age(db_session, timedelta(hours=3))

    # retried after the short block (the retry is the monitor's own grab);
    # the third failure in a row is not transient any more
    assert await _grab(db_session, series_id) == 1
    retry = (await db_session.execute(
        select(Download).where(Download.status == DownloadStatus.QUEUED)
    )).scalar_one()
    await _fail(db_session, retry, monkeypatch, httpx.ConnectError("connection refused"))

    await _age(db_session, timedelta(hours=3))
    assert await _grab(db_session, series_id) == 0


async def test_manual_retry_clears_the_transient_marker(
    db_session, tmp_path, monkeypatch, source
):
    from mangarr.api import queue

    series_id = await _make_series(db_session, tmp_path)
    dl = await _fail(db_session, await _queue(db_session, series_id), monkeypatch,
                     httpx.ConnectError("connection refused"))
    await queue.retry_failed_download(dl.id, db_session)
    # the retried row now fails for a reason that has nothing to do with the
    # network; it must not inherit the short block
    await _fail(db_session, dl, monkeypatch, ValueError("unexpected payload"))

    await _age(db_session, timedelta(hours=3))
    assert await _grab(db_session, series_id) == 0


async def test_failure_stays_in_activity_while_it_blocks(
    db_session, tmp_path, monkeypatch, source
):
    series_id = await _make_series(db_session, tmp_path)
    dl = await _fail(db_session, await _queue(db_session, series_id), monkeypatch,
                     ValueError("unexpected payload"))

    await _age(db_session, timedelta(days=3))  # past the 48h floor, still blocking
    assert [item.id for item in await get_queue(db_session)] == [dl.id]

    await _age(db_session, tasks.FAILED_GRAB_RETRY_AFTER + timedelta(days=1))
    assert await get_queue(db_session) == []


async def test_transient_failure_stays_in_activity_for_the_usual_window(
    db_session, tmp_path, monkeypatch, source
):
    series_id = await _make_series(db_session, tmp_path)
    dl = await _fail(db_session, await _queue(db_session, series_id), monkeypatch,
                     httpx.ConnectError("connection refused"))

    await _age(db_session, timedelta(hours=24))  # no longer blocking, still shown
    assert [item.id for item in await get_queue(db_session)] == [dl.id]

    await _age(db_session, timedelta(days=3))
    assert await get_queue(db_session) == []


async def test_existing_database_gains_the_failure_kind_column(tmp_path, monkeypatch):
    from mangarr import db

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mangarr.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        info = await conn.exec_driver_sql("PRAGMA table_info(downloads)")
        if "transient_error" in {row[1] for row in info.fetchall()}:
            # make it a database from before the column existed
            await conn.exec_driver_sql("ALTER TABLE downloads DROP COLUMN transient_error")
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db.config, "data_dir", tmp_path)

    await db.init_db()

    async with engine.connect() as conn:
        info = await conn.exec_driver_sql("PRAGMA table_info(downloads)")
        columns = {row[1]: row for row in info.fetchall()}
    await engine.dispose()
    assert "transient_error" in columns
    _, _, _, notnull, default, _ = columns["transient_error"]
    assert (notnull, default) == (1, "0")
