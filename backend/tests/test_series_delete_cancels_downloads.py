"""Deleting a series must stop its direct downloads (the same "removed by
user" cancellation the queue uses) instead of letting the worker finish a
chapter into a series that no longer exists. Torrents are left alone."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.api import series as series_api
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
from mangarr.schemas import SeriesBulkIn
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


async def _make_series(db_session, root, title="Test Series"):
    series = Series(title=title, sort_title=title.lower(), root_folder=root, folder_name=title)
    series.chapters = [Chapter(number=1.0, monitored=True), Chapter(number=2.0, monitored=True)]
    db_session.add(series)
    await db_session.commit()
    return series


async def _add_download(db_session, series, chapter, kind, status):
    dl = Download(series_id=series.id, chapter_id=chapter.id if chapter else None,
                  kind=kind, status=status, source_name="fake", payload="c1",
                  title=f"{series.title} download",
                  torrent_hash="a" * 40 if chapter is None else "")
    db_session.add(dl)
    await db_session.commit()
    return dl


async def _states(db_session):
    rows = await db_session.execute(select(Download.id, Download.status, Download.error))
    return {i: (status, error) for i, status, error in rows.all()}


async def _seed(db_session, tmp_path):
    root = RootFolder(path=str(tmp_path))
    doomed = await _make_series(db_session, root, "Doomed")
    kept = await _make_series(db_session, root, "Kept")
    ids = {
        "queued": await _add_download(db_session, doomed, doomed.chapters[0],
                                      DownloadKind.DIRECT, DownloadStatus.QUEUED),
        "running": await _add_download(db_session, doomed, doomed.chapters[1],
                                       DownloadKind.DIRECT, DownloadStatus.DOWNLOADING),
        "done": await _add_download(db_session, doomed, doomed.chapters[1],
                                    DownloadKind.DIRECT, DownloadStatus.DONE),
        "torrent": await _add_download(db_session, doomed, None,
                                       DownloadKind.TORRENT, DownloadStatus.DOWNLOADING),
        "other": await _add_download(db_session, kept, kept.chapters[0],
                                     DownloadKind.DIRECT, DownloadStatus.QUEUED),
    }
    return doomed, {name: dl.id for name, dl in ids.items()}


def _assert_only_direct_work_cancelled(states, ids):
    removed = (DownloadStatus.FAILED, REMOVED_BY_USER)
    assert states[ids["queued"]] == removed
    assert states[ids["running"]] == removed
    assert states[ids["done"]][0] == DownloadStatus.DONE
    assert states[ids["torrent"]][0] == DownloadStatus.DOWNLOADING
    assert states[ids["other"]][0] == DownloadStatus.QUEUED


async def test_deleting_a_series_cancels_its_direct_downloads(db_session, tmp_path):
    doomed, ids = await _seed(db_session, tmp_path)

    await series_api.delete_series(doomed.id, db_session)

    _assert_only_direct_work_cancelled(await _states(db_session), ids)


async def test_bulk_delete_cancels_direct_downloads_too(db_session, tmp_path):
    doomed, ids = await _seed(db_session, tmp_path)

    await series_api.delete_series_bulk(SeriesBulkIn(series_ids=[doomed.id]), db_session)

    _assert_only_direct_work_cancelled(await _states(db_session), ids)


async def test_running_download_stops_when_its_series_is_deleted(
    db_session, tmp_path, monkeypatch
):
    series = await _make_series(db_session, RootFolder(path=str(tmp_path)))
    dl = await _add_download(db_session, series, series.chapters[0],
                             DownloadKind.DIRECT, DownloadStatus.QUEUED)
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    written = []

    async def download_while_series_is_deleted(source, payload, series, chapter, dest,
                                               progress_cb=None, cancel_cb=None, web_url=""):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"cbz")
        written.append(dest)
        await series_api.delete_series(series.id, db_session)

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", download_while_series_is_deleted)

    await tasks._run_direct_download(db_session, dl)

    assert written and not written[0].exists()  # nothing left in the deleted series' folder
    assert (await _states(db_session))[dl.id] == (DownloadStatus.FAILED, REMOVED_BY_USER)
