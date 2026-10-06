"""A chapter resync deletes the series' chapters and their download records.
It must refuse while chapter downloads are in flight (their worker doesn't
hold the series lock), and a direct download whose record vanishes anyway
must stop cleanly instead of committing against deleted rows."""

import pytest
from fastapi import HTTPException
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.api.library import resync_chapters
from mangarr.jobs import tasks
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


async def _make_series(db_session, tmp_path):
    series = Series(
        title="Test Series", sort_title="test series",
        root_folder=RootFolder(path=str(tmp_path)), folder_name="Test Series",
    )
    chapter = Chapter(number=1.0, monitored=True)
    series.chapters.append(chapter)
    db_session.add(series)
    await db_session.commit()
    return series, chapter


async def _add_download(db_session, series, chapter=None, **kw):
    dl = Download(
        series_id=series.id, chapter_id=chapter.id if chapter else None,
        source_name="fake", payload="c1", title="Test Series - Chapter 1", **kw,
    )
    db_session.add(dl)
    await db_session.commit()
    return dl


async def _count(db_session, model):
    return await db_session.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize("status", [DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING])
async def test_resync_refuses_while_a_chapter_download_is_in_flight(
    db_session, tmp_path, status
):
    series, chapter = await _make_series(db_session, tmp_path)
    await _add_download(db_session, series, chapter, kind=DownloadKind.DIRECT, status=status)

    with pytest.raises(HTTPException) as err:
        await resync_chapters(series.id, db_session)

    assert err.value.status_code == 409
    assert "in progress" in err.value.detail
    # nothing was torn down
    assert await _count(db_session, Download) == 1
    assert await _count(db_session, Chapter) == 1


async def test_resync_refuses_while_a_torrent_is_importing(db_session, tmp_path):
    series, _ = await _make_series(db_session, tmp_path)
    await _add_download(db_session, series, kind=DownloadKind.TORRENT,
                        status=DownloadStatus.IMPORTING, torrent_hash="a" * 40)

    with pytest.raises(HTTPException) as err:
        await resync_chapters(series.id, db_session)

    assert err.value.status_code == 409


async def test_resync_still_runs_around_a_transferring_torrent(db_session, tmp_path):
    """Series-level torrents still downloading survive a resync by design —
    they don't block it either."""
    series, chapter = await _make_series(db_session, tmp_path)
    torrent = await _add_download(db_session, series, kind=DownloadKind.TORRENT,
                                  status=DownloadStatus.DOWNLOADING, torrent_hash="a" * 40)
    await _add_download(db_session, series, chapter, kind=DownloadKind.DIRECT,
                        status=DownloadStatus.DONE)

    await resync_chapters(series.id, db_session)

    ids = (await db_session.execute(select(Download.id))).scalars().all()
    assert ids == [torrent.id]


async def test_direct_download_whose_record_is_deleted_stops_and_cleans_up(
    db_session, tmp_path, monkeypatch
):
    series, chapter = await _make_series(db_session, tmp_path)
    dl = await _add_download(db_session, series, chapter, kind=DownloadKind.DIRECT,
                             status=DownloadStatus.QUEUED)
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    written = []

    async def download_then_lose_record(source, payload, series, chapter, dest,
                                        progress_cb=None, cancel_cb=None, web_url=""):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"cbz")
        written.append(dest)
        # what a concurrent resync does, from outside the worker's view
        for model in (Download, Chapter):
            await db_session.execute(
                sa_delete(model).execution_options(synchronize_session=False)
            )
        await db_session.commit()

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", download_then_lose_record)

    await tasks._run_direct_download(db_session, dl)

    assert written and not written[0].exists()  # no orphaned file left behind
    assert await _count(db_session, Download) == 0
