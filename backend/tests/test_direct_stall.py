"""Direct-download worker robustness: a stalled download must fail visibly
instead of wedging the single queue worker, concurrent page callbacks must
not corrupt the shared session, and failures must stay visible in /queue."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.api.queue import _remove_downloads, get_queue
from mangarr.jobs import tasks
from mangarr.jobs.tasks import REMOVED_BY_USER
from mangarr.models import (
    Base,
    Chapter,
    Download,
    DownloadKind,
    DownloadStatus,
    RootFolder,
    Series,
)
from mangarr.sources.base import DirectSource, SourceChapter


class FakeSource(DirectSource):
    name = "fake"

    async def search_series(self, query):
        return []

    async def list_chapters(self, external_id):
        return [SourceChapter(source_name=self.name, external_id="c1", number=1.0)]

    async def get_pages(self, chapter_external_id):
        return []


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _make_download(db_session, tmp_path):
    root = RootFolder(path=str(tmp_path))
    series = Series(
        title="Test Series",
        sort_title="test series",
        root_folder=root,
        folder_name="Test Series",
    )
    chapter = Chapter(number=1.0, monitored=True)
    series.chapters.append(chapter)
    db_session.add(series)
    await db_session.commit()
    dl = Download(
        series_id=series.id,
        chapter_id=chapter.id,
        kind=DownloadKind.DIRECT,
        status=DownloadStatus.QUEUED,
        source_name="fake",
        payload="c1",
        title="Test Series - Chapter 1",
    )
    db_session.add(dl)
    await db_session.commit()
    return dl


async def test_stalled_download_fails_visibly_instead_of_hanging(
    db_session, tmp_path, monkeypatch
):
    dl = await _make_download(db_session, tmp_path)
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    monkeypatch.setattr(tasks, "DIRECT_STALL_TIMEOUT", timedelta(seconds=0.2))

    cancelled = asyncio.Event()

    async def hanging_download(source, payload, series, chapter, dest,
                               progress_cb=None, cancel_cb=None, web_url=""):
        try:
            await asyncio.Event().wait()  # hangs forever
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", hanging_download)

    # the whole point: this returns instead of blocking the queue forever
    await asyncio.wait_for(tasks._run_direct_download(db_session, dl), timeout=10)
    await db_session.refresh(dl)

    assert cancelled.is_set()
    assert dl.status == DownloadStatus.FAILED
    assert "stalled" in dl.error


async def test_concurrent_page_callbacks_do_not_break_the_session(
    db_session, tmp_path, monkeypatch
):
    """Page fetches run concurrently and call cancel/progress callbacks that
    hit the worker's session; unserialized, that raises 'session in prepared
    state' and fails the download."""
    dl = await _make_download(db_session, tmp_path)
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())

    total = 30

    async def concurrent_download(source, payload, series, chapter, dest,
                                  progress_cb=None, cancel_cb=None, web_url=""):
        async def page(i):
            await cancel_cb()
            await progress_cb(i + 1, total)
            await cancel_cb()

        async with asyncio.TaskGroup() as tg:
            for i in range(total):
                tg.create_task(page(i))

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", concurrent_download)

    await asyncio.wait_for(tasks._run_direct_download(db_session, dl), timeout=10)
    await db_session.refresh(dl)

    assert dl.error == ""
    assert dl.status == DownloadStatus.DONE


async def test_failed_downloads_stay_visible_in_queue(db_session, tmp_path):
    dl = await _make_download(db_session, tmp_path)
    dl.status = DownloadStatus.FAILED
    dl.error = "stalled: no page finished for 15 minutes"
    await db_session.commit()

    items = await get_queue(db_session)
    assert [i.id for i in items] == [dl.id]
    assert items[0].error == dl.error

    # dismissing the failure hides it and clears the retry blacklist marker
    assert await _remove_downloads(db_session, [dl.id]) == 1
    await db_session.refresh(dl)
    assert dl.error == REMOVED_BY_USER
    assert await get_queue(db_session) == []
