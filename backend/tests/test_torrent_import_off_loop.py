"""A torrent import may copy or pack gigabytes; it must run off the event
loop so the API, the direct queue and the qBittorrent sync keep going, and
its results must still land on the session's chapters."""

import asyncio
import threading

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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

VALUES = {
    "naming_template": "{series} - Ch. {chapter:04.1f}",
    "naming_template_no_volume": "{series} - Ch. {chapter:04.1f}",
    "import_mode": "copy",
}


@pytest.fixture
async def db_session(monkeypatch):
    monkeypatch.setattr(tasks.notifications, "notify_import", lambda *a: None)
    monkeypatch.setattr(tasks, "_notify_kavita", lambda *a: None)
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def test_slow_import_does_not_block_the_event_loop(db_session, tmp_path, monkeypatch):
    (tmp_path / "library").mkdir()  # the API creates a root on add
    series = Series(title="Vinland Saga", sort_title="vinland saga",
                    root_folder=RootFolder(path=str(tmp_path / "library")),
                    folder_name="Vinland Saga")
    series.chapters = [Chapter(number=1.0), Chapter(number=2.0)]
    db_session.add(series)
    await db_session.commit()
    dl = Download(series_id=series.id, kind=DownloadKind.TORRENT,
                  status=DownloadStatus.IMPORTING, title="Vinland Saga c002")
    db_session.add(dl)
    await db_session.commit()
    content = tmp_path / "payload"
    content.mkdir()
    dest = tmp_path / "library" / "Vinland Saga" / "Vinland Saga - Ch. 02.0.cbz"

    import_started = threading.Event()
    loop_ran = threading.Event()
    ran_during_import = []

    def slow_import(content_path, series_snapshot, chapters, *args, **kwargs):
        # stands in for a long copy: it blocks until another coroutine has
        # run, which can only happen if it isn't blocking the loop itself
        import_started.set()
        ran_during_import.append(loop_ran.wait(timeout=0.5))
        match = next(c for c in chapters if c.number == 2.0)
        return [(dest, match, None)]

    monkeypatch.setattr(tasks, "import_torrent_payload", slow_import)

    async def other_work():
        while not import_started.is_set():
            await asyncio.sleep(0.001)
        loop_ran.set()

    await asyncio.wait_for(asyncio.gather(
        tasks._import_torrent(db_session, dl, content, VALUES),
        other_work(),
    ), timeout=5)

    assert ran_during_import == [True]
    await db_session.refresh(dl)
    assert dl.status == DownloadStatus.DONE
    by_number = {c.number: c for c in series.chapters}
    assert by_number[2.0].downloaded and by_number[2.0].file_path == str(dest)
    assert not by_number[1.0].downloaded


@pytest.fixture
async def two_sessions(tmp_path, monkeypatch):
    """The sync's session and an API request's, on separate connections."""
    notified = []
    monkeypatch.setattr(tasks.notifications, "notify_import", lambda *a: notified.append(a))
    monkeypatch.setattr(tasks, "_notify_kavita", lambda *a: None)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as sync_session, maker() as api_session:
        yield sync_session, api_session, notified
    await engine.dispose()


@pytest.mark.parametrize("outcome", ["imported", "payload deleted", "failed"])
async def test_removal_during_import_stands(two_sessions, tmp_path, monkeypatch, outcome):
    """The import no longer blocks the queue, so the user can remove the
    download while it runs; whatever the import then does must not overwrite
    that removal or record its chapters."""
    from unittest.mock import AsyncMock, MagicMock

    from mangarr import settings_service
    from mangarr.api import queue

    session, api_session, notified = two_sessions
    (tmp_path / "library").mkdir()  # the API creates a root on add
    series = Series(title="Vinland Saga", sort_title="vinland saga",
                    root_folder=RootFolder(path=str(tmp_path / "library")),
                    folder_name="Vinland Saga")
    series.chapters = [Chapter(number=1.0), Chapter(number=2.0)]
    session.add(series)
    await session.commit()
    dl = Download(series_id=series.id, kind=DownloadKind.TORRENT,
                  status=DownloadStatus.IMPORTING, title="Vinland Saga c002",
                  torrent_hash="a" * 40)
    session.add(dl)
    await session.commit()
    content = tmp_path / "payload"
    content.mkdir()
    dest = tmp_path / "library" / "Vinland Saga" / "Vinland Saga - Ch. 02.0.cbz"

    import_started = threading.Event()
    removal_committed = threading.Event()

    def import_outlived_by_removal(content_path, series_snapshot, chapters, *args, **kwargs):
        import_started.set()
        assert removal_committed.wait(timeout=5)
        if outcome == "payload deleted":
            raise FileNotFoundError("torrent file missing: payload/c002.cbz")
        if outcome == "failed":
            raise OSError("Incomplete library copy")
        match = next(c for c in chapters if c.number == 2.0)
        return [(dest, match, None)]

    monkeypatch.setattr(tasks, "import_torrent_payload", import_outlived_by_removal)
    monkeypatch.setattr(tasks, "_import_path_missing_counts", {})
    values = dict(settings_service.DEFAULTS, qbittorrent_enabled="true")
    monkeypatch.setattr(queue.registry, "apply_settings", AsyncMock(return_value=values))
    client = MagicMock(delete_torrents=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(queue, "QbtClient", lambda *args: client)

    async def remove_while_importing():
        assert await asyncio.to_thread(import_started.wait, 5)
        assert await queue._remove_downloads(api_session, [dl.id]) == 1
        removal_committed.set()

    await asyncio.wait_for(asyncio.gather(
        tasks._import_torrent(session, dl, content, VALUES),
        remove_while_importing(),
    ), timeout=10)

    row = (await api_session.execute(
        select(Download.status, Download.error).where(Download.id == dl.id)
    )).one()
    assert tuple(row) == (DownloadStatus.FAILED, tasks.REMOVED_BY_USER)
    chapter = await api_session.scalar(
        select(Chapter).where(Chapter.series_id == series.id, Chapter.number == 2.0)
    )
    assert not chapter.downloaded and not chapter.file_path
    assert await api_session.scalar(
        select(HistoryEvent.id).where(HistoryEvent.event == "imported")
    ) is None
    assert notified == []
    assert dl.id not in tasks._import_path_missing_counts


async def test_importer_errors_still_fail_the_download(db_session, tmp_path, monkeypatch):
    (tmp_path / "library").mkdir()  # the API creates a root on add
    series = Series(title="Vinland Saga", sort_title="vinland saga",
                    root_folder=RootFolder(path=str(tmp_path / "library")),
                    folder_name="Vinland Saga")
    db_session.add(series)
    await db_session.commit()
    dl = Download(series_id=series.id, kind=DownloadKind.TORRENT,
                  status=DownloadStatus.IMPORTING, title="Vinland Saga pack")
    db_session.add(dl)
    await db_session.commit()
    content = tmp_path / "payload"
    content.mkdir()

    def broken_import(*args, **kwargs):
        raise OSError("Incomplete library copy")

    monkeypatch.setattr(tasks, "import_torrent_payload", broken_import)

    await tasks._import_torrent(db_session, dl, content, VALUES)

    await db_session.refresh(dl)
    assert dl.status == DownloadStatus.FAILED
    assert dl.error == "Incomplete library copy"


async def test_snapshot_carries_what_the_duplicate_check_reads(db_session, tmp_path):
    """The real importer compares a payload against the chapters already on
    disk, so the plain snapshots it gets must carry their files."""
    import zipfile

    def make_cbz(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("001.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    existing = tmp_path / "library" / "Vinland Saga" / "chapter-1.cbz"
    make_cbz(existing)
    series = Series(title="Vinland Saga", sort_title="vinland saga",
                    root_folder=RootFolder(path=str(tmp_path / "library")),
                    folder_name="Vinland Saga")
    series.chapters = [Chapter(number=1.0, downloaded=True, file_path=str(existing)),
                       Chapter(number=2.0)]
    db_session.add(series)
    await db_session.commit()
    dl = Download(series_id=series.id, kind=DownloadKind.TORRENT,
                  status=DownloadStatus.IMPORTING, title="Vinland Saga c002")
    db_session.add(dl)
    await db_session.commit()
    payload = tmp_path / "payload" / "Vinland Saga - Ch. 002.cbz"
    make_cbz(payload)

    await tasks._import_torrent(db_session, dl, payload, VALUES)

    await db_session.refresh(dl)
    assert dl.status == DownloadStatus.FAILED
    assert "same page images as existing chapter 1" in dl.error


@pytest.mark.parametrize("stem,expected", [
    ("Series c001-010", {1., 2., 2.5, 3., 4., 5., 6., 7., 8., 9., 10.}),
    ("Series v01-03 (Digital)", {1., 2., 2.5, 3., 4., 5., 6., 7., 8., 9., 10., 11.}),
    ("Series v02 (c001-006)", {1., 2., 2.5, 3., 4., 5., 6.}),
    ("Series v01 (2009) (Digital) (c1fi7)", {1., 2., 2.5, 3., 4., 5., 6.}),
])
async def test_range_import_records_every_covered_chapter_and_does_not_regrab(
    db_session, tmp_path, stem, expected
):
    import zipfile

    from mangarr.library.scanner import scan_series
    from mangarr.sources.base import TorrentRelease
    from mangarr.torrent_selection import select_best_torrent
    from tests.test_torrent_selection import FakeIndexer, torrent_with

    library = tmp_path / "library"
    library.mkdir()
    series = Series(title="Series", sort_title="series", folder_name="Series",
                    root_folder=RootFolder(path=str(library)))
    series.chapters = [Chapter(number=n, volume=1 if n <= 6 else 2)
                       for n in [1., 2., 2.5, 3., 4., 5., 6., 7., 8., 9., 10., 11.]]
    excluded = Chapter(number=3.5, volume=1, excluded=True)
    series.chapters.append(excluded)
    db_session.add(series)
    await db_session.commit()
    dl = Download(series_id=series.id, kind=DownloadKind.TORRENT,
                  status=DownloadStatus.IMPORTING, title=stem)
    db_session.add(dl)
    await db_session.commit()
    source = tmp_path / "payload" / (stem + ".cbz")
    source.parent.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.png", b"first page")
        archive.writestr("002.png", b"last page")
        archive.writestr("ComicInfo.xml", "<ComicInfo><Number>1</Number></ComicInfo>")

    await tasks._import_torrent(db_session, dl, source, VALUES)
    await db_session.refresh(dl)
    await db_session.refresh(series, ["chapters"])
    assert dl.status == DownloadStatus.DONE
    owned = [c for c in series.chapters if c.downloaded]
    assert {c.number for c in owned} == expected
    assert len({c.file_path for c in owned}) == 1
    dest = next((library / "Series").iterdir())
    assert dest.read_bytes() == source.read_bytes()
    assert all(c.file_path == str(dest) and c.file_source == "nyaa" for c in owned)
    assert not excluded.downloaded and not excluded.file_path
    if "-" in stem or "c001-006" in stem:
        assert dest.name == source.name

    tracked = [c for c in series.chapters if not c.excluded]
    scan = scan_series(series, tracked, [dest.parent])
    assert scan.matched_chapters == 0 and scan.cleared == 0
    await tasks._import_torrent(db_session, dl, source, VALUES)
    assert list(dest.parent.iterdir()) == [dest]
    assert {c.number for c in series.chapters if c.downloaded} == expected
    assert all(c.file_source == "nyaa" for c in owned)

    release = TorrentRelease(source_name="fake", title=stem, magnet="magnet:range",
                             torrent_url="https://example.test/range.torrent",
                             size_bytes=100, seeders=10)
    indexer = FakeIndexer([release], {release.magnet: torrent_with(dest.name)})
    assert await select_best_torrent(series, series.chapters, [indexer],
                                     max_size_bytes=1000, min_seeders=1) is None
