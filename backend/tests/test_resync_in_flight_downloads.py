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


async def test_torrent_finishing_mid_resync_waits_for_the_rebuild(tmp_path, monkeypatch):
    """The resync's IMPORTING guard runs under the series lock; a torrent that
    finishes after it, while the chapters are torn down and being rebuilt,
    must not import against the empty list — it waits for a later sync."""
    import asyncio
    import zipfile
    from contextlib import asynccontextmanager

    import respx

    from mangarr.library.naming import DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME

    qbt_url, torrent_hash = "http://qbt:8080", "d" * 40
    values = {
        "qbittorrent_enabled": "true", "qbittorrent_url": qbt_url,
        "qbittorrent_username": "admin", "qbittorrent_password": "pw",
        "naming_template": DEFAULT_TEMPLATE,
        "naming_template_no_volume": DEFAULT_TEMPLATE_NO_VOLUME,
        "import_mode": "hardlink",
    }
    # the API request and the sync job each have their own session
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mangarr.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session

    async def apply_settings(session):
        return dict(values)

    torn_down, rebuild = asyncio.Event(), asyncio.Event()

    async def rebuild_from_sources(session, series, values, *args, **kwargs):
        # the real one fetches every linked source: the slow part of a resync
        torn_down.set()
        await rebuild.wait()
        series.chapters.extend(Chapter(number=float(n)) for n in (1, 2, 3))

    monkeypatch.setattr(tasks, "_SERIES_LOCKS", {})
    monkeypatch.setattr(tasks, "session_scope", scope)
    monkeypatch.setattr(tasks.registry, "apply_settings", apply_settings)
    monkeypatch.setattr(tasks, "update_chapters", rebuild_from_sources)
    monkeypatch.setattr(tasks.notifications, "notify_import", lambda *a: None)
    monkeypatch.setattr(tasks, "_notify_kavita", lambda *a: None)

    library = tmp_path / "library"
    library.mkdir()
    category = tmp_path / "downloads"
    category.mkdir()
    payload = category / "Kagurabachi - c003.cbz"
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("001.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    async with maker() as setup:
        series = Series(title="Kagurabachi", sort_title="kagurabachi",
                        root_folder=RootFolder(path=str(library)), folder_name="Kagurabachi")
        series.chapters = [Chapter(number=float(n)) for n in (1, 2, 3)]
        setup.add(series)
        await setup.commit()
        dl = Download(series_id=series.id, kind=DownloadKind.TORRENT,
                      status=DownloadStatus.DOWNLOADING, title="Kagurabachi c003",
                      torrent_hash=torrent_hash)
        setup.add(dl)
        await setup.commit()
        series_id, dl_id = series.id, dl.id

    async def qbt_sync():
        """One sync pass against a qBittorrent reporting the torrent finished."""
        with respx.mock(base_url=f"{qbt_url}/api/v2", assert_all_called=False) as qbt:
            qbt.post("/auth/login").respond(200, text="Ok.")
            qbt.get("/torrents/info").respond(json=[{
                "hash": torrent_hash, "name": "Kagurabachi c003", "progress": 1.0,
                "state": "stalledUP", "category": "mangarr",
                "content_path": str(payload), "save_path": str(category),
            }])
            qbt.get("/torrents/files").respond(
                json=[{"name": payload.name, "priority": 1}])
            await tasks.sync_qbittorrent()
        async with maker() as check:
            return await check.get(Download, dl_id)

    folder = library / "Kagurabachi"
    try:
        async with maker() as api_session:
            resync = asyncio.create_task(resync_chapters(series_id, api_session))
            await asyncio.wait_for(torn_down.wait(), timeout=5)
            # the torrent finishes while the chapter list is being rebuilt
            mid = await qbt_sync()
            assert mid.status == DownloadStatus.DOWNLOADING
            assert not folder.exists() or not any(folder.iterdir())
            rebuild.set()
            await asyncio.wait_for(resync, timeout=5)

        done = await qbt_sync()
        assert done.status == DownloadStatus.DONE, done.error
        assert sorted(p.name for p in folder.iterdir()) == ["Kagurabachi - Ch. 0003.cbz"]
        async with maker() as check:
            chapter = await check.scalar(
                select(Chapter).where(Chapter.series_id == series_id, Chapter.number == 3.0))
            assert chapter.downloaded and chapter.file_source == "nyaa"
        assert not tasks._SERIES_LOCKS[series_id].locked()  # released after the import
    finally:
        rebuild.set()
        await engine.dispose()
