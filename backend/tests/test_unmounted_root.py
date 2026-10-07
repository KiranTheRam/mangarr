"""A library root that is missing or unmounted must not read as an empty
library: that cleared every chapter and the monitor re-downloaded it all."""

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from mangarr.jobs import tasks
from mangarr.library.scanner import scan_series
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


def _owned_chapters(root, count=3):
    folder = root / "Series"
    return [
        Chapter(id=n, number=float(n), downloaded=True,
                file_path=str(folder / f"Series - Ch. {n:04d}.cbz"))
        for n in range(1, count + 1)
    ]


def test_missing_root_is_unavailable(tmp_path):
    from mangarr.library.scanner import root_unavailable
    assert "missing" in root_unavailable(tmp_path / "unmounted")


def test_empty_root_is_unavailable_only_when_files_are_expected(tmp_path):
    from mangarr.library.scanner import root_unavailable
    assert "empty" in root_unavailable(tmp_path, expect_content=True)
    # a brand-new library root is legitimately empty
    assert root_unavailable(tmp_path, expect_content=False) == ""


@pytest.mark.parametrize("mounted_as", ["missing", "empty"])
def test_scan_leaves_chapters_alone_when_root_is_unavailable(tmp_path, mounted_as):
    root = tmp_path / "manga"
    if mounted_as == "empty":
        root.mkdir()
    chapters = _owned_chapters(root)

    result = scan_series(Series(title="Series"), chapters, [root / "Series"], root=root)

    assert result.root_unavailable
    assert result.cleared == 0
    assert all(c.downloaded and c.file_path for c in chapters)


def test_scan_still_clears_when_only_the_series_folder_is_gone(tmp_path):
    # the root is mounted (other series are there) but this series' folder was
    # deleted: its chapters really are gone and should be wanted again
    root = tmp_path / "manga"
    (root / "Other Series").mkdir(parents=True)
    chapters = _owned_chapters(root)

    result = scan_series(Series(title="Series"), chapters, [root / "Series"], root=root)

    assert result.root_unavailable == ""
    assert result.cleared == 3
    assert not any(c.downloaded for c in chapters)


class FakeSession:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


async def test_reconcile_leaves_chapters_alone_when_root_is_missing(tmp_path):
    root = tmp_path / "manga"
    series = Series(id=1, title="Series", root_folder=RootFolder(path=str(root)))
    series.chapters = _owned_chapters(root)
    session = FakeSession()

    assert await tasks.reconcile_downloaded_files(session, series) == 0

    assert session.commits == 0
    assert all(c.downloaded for c in series.chapters)


class FakeSource(DirectSource):
    name = "fake"

    async def search_series(self, query):
        return []

    async def list_chapters(self, external_id):
        return [SourceChapter(source_name=self.name, external_id="c4", number=4.0)]

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


async def _series_in(session, root):
    """A series with three chapters on disk (per the DB) and a missing 4th."""
    series = Series(title="Series", sort_title="series", folder_name="Series",
                    root_folder=RootFolder(path=str(root)))
    series.source_links.append(SeriesSourceLink(source_name="fake", external_id="x"))
    for chapter in _owned_chapters(root):
        chapter.id = None
        series.chapters.append(chapter)
    series.chapters.append(Chapter(number=4.0, monitored=True))
    session.add(series)
    await session.commit()
    result = await session.execute(
        select(Series)
        .options(selectinload(Series.chapters), selectinload(Series.source_links),
                 selectinload(Series.root_folder), selectinload(Series.extra_folders))
        .where(Series.id == series.id)
    )
    return result.scalar_one()


async def test_no_grabs_while_root_is_missing(db_session, tmp_path, monkeypatch):
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    series = await _series_in(db_session, tmp_path / "manga")

    assert await tasks.grab_missing_chapters(db_session, series, {}) == 0

    assert (await db_session.execute(select(Download))).scalars().all() == []


async def test_direct_download_does_not_write_into_a_missing_root(
    db_session, tmp_path, monkeypatch
):
    root = tmp_path / "manga"
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    series = await _series_in(db_session, root)
    missing = next(c for c in series.chapters if not c.downloaded)
    dl = Download(series_id=series.id, chapter_id=missing.id, kind=DownloadKind.DIRECT,
                  status=DownloadStatus.QUEUED, source_name="fake", payload="c4",
                  title="Series - Chapter 4")
    db_session.add(dl)
    await db_session.commit()
    started = []

    async def fake_download(*args, **kwargs):
        started.append(args)

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", fake_download)

    await tasks._run_direct_download(db_session, dl)
    await db_session.refresh(dl)

    assert started == []
    assert not root.exists()
    assert dl.status == DownloadStatus.FAILED
    assert "missing" in dl.error


async def test_torrent_import_waits_for_a_missing_root(db_session, tmp_path):
    root = tmp_path / "manga"
    series = await _series_in(db_session, root)
    payload = tmp_path / "downloads" / "Series c004.cbz"
    payload.parent.mkdir()
    payload.write_bytes(b"cbz")
    dl = Download(series_id=series.id, kind=DownloadKind.TORRENT,
                  status=DownloadStatus.IMPORTING, torrent_hash="a" * 40,
                  title="Series c004")
    db_session.add(dl)
    await db_session.commit()

    await tasks._import_torrent(db_session, dl, payload, {})

    assert not root.exists()
    assert dl.status == DownloadStatus.DOWNLOADING
    assert "waiting for the library" in dl.error


# ---- a root shared with series that own files

async def _add_series(session, root_folder, title, owned=0):
    """A series in `root_folder` owning `owned` chapter files (per the DB)
    plus a missing, monitored chapter 4 (the one FakeSource lists)."""
    folder = Path(root_folder.path) / title
    series = Series(title=title, sort_title=title.lower(), folder_name=title,
                    root_folder=root_folder)
    series.source_links.append(SeriesSourceLink(source_name="fake", external_id=title))
    for n in range(1, owned + 1):
        series.chapters.append(Chapter(
            number=float(n), downloaded=True,
            file_path=str(folder / f"{title} - Ch. {n:04d}.cbz")))
    series.chapters.append(Chapter(number=4.0, monitored=True))
    session.add(series)
    await session.commit()
    return await tasks._load_series(session, series.id)


async def test_new_series_does_not_write_into_an_empty_root_others_own(
    db_session, tmp_path, monkeypatch
):
    # the share is unmounted, leaving an empty mount point; the series that
    # owns files there is protected, so the new one must be too — its write
    # would make the root look mounted and clear the neighbour on next scan
    root = tmp_path / "manga"
    root.mkdir()
    shared = RootFolder(path=str(root))
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    owner = await _add_series(db_session, shared, "Owner", owned=3)
    newcomer = await _add_series(db_session, shared, "Newcomer")
    wanted = next(c for c in newcomer.chapters if c.number == 4.0)
    dl = Download(series_id=newcomer.id, chapter_id=wanted.id, kind=DownloadKind.DIRECT,
                  status=DownloadStatus.QUEUED, source_name="fake", payload="c4",
                  title="Newcomer - Chapter 4")
    db_session.add(dl)
    await db_session.commit()

    async def fake_download(source, payload, series, chapter, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"cbz")

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", fake_download)

    await tasks._run_direct_download(db_session, dl)
    await db_session.refresh(dl)

    assert list(root.iterdir()) == []
    assert dl.status == DownloadStatus.FAILED
    assert "empty" in dl.error
    owner = await tasks._load_series(db_session, owner.id)
    assert await tasks.reconcile_downloaded_files(db_session, owner) == 0
    await tasks.scan_series_folder(db_session, owner)
    assert sum(c.downloaded for c in owner.chapters) == 3


async def test_no_grabs_for_a_new_series_in_an_empty_root_others_own(
    db_session, tmp_path, monkeypatch
):
    root = tmp_path / "manga"
    root.mkdir()
    shared = RootFolder(path=str(root))
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    await _add_series(db_session, shared, "Owner", owned=3)
    newcomer = await _add_series(db_session, shared, "Newcomer")
    values = await tasks.registry.apply_settings(db_session)

    assert await tasks.grab_missing_chapters(db_session, newcomer, values) == 0

    assert (await db_session.execute(select(Download))).scalars().all() == []


async def test_torrent_import_for_a_new_series_waits_for_a_root_others_own(
    db_session, tmp_path
):
    root = tmp_path / "manga"
    root.mkdir()
    shared = RootFolder(path=str(root))
    await _add_series(db_session, shared, "Owner", owned=3)
    newcomer = await _add_series(db_session, shared, "Newcomer")
    payload = tmp_path / "downloads" / "Newcomer c004.cbz"
    payload.parent.mkdir()
    payload.write_bytes(b"cbz")
    dl = Download(series_id=newcomer.id, kind=DownloadKind.TORRENT,
                  status=DownloadStatus.IMPORTING, torrent_hash="b" * 40,
                  title="Newcomer c004")
    db_session.add(dl)
    await db_session.commit()

    values = await tasks.registry.apply_settings(db_session)

    await tasks._import_torrent(db_session, dl, payload, values)

    assert list(root.iterdir()) == []
    assert dl.status == DownloadStatus.DOWNLOADING


async def test_a_brand_new_root_is_still_writable(db_session, tmp_path, monkeypatch):
    # nothing in the root owns files: an empty root is just a new library
    root = tmp_path / "manga"
    root.mkdir()
    shared = RootFolder(path=str(root))
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    await _add_series(db_session, shared, "Other")
    newcomer = await _add_series(db_session, shared, "Newcomer")
    values = await tasks.registry.apply_settings(db_session)

    assert await tasks.grab_missing_chapters(db_session, newcomer, values) == 1


# ---- library endpoints refuse up front instead of losing downloaded state

class VolumeSource(FakeSource):
    """Lists chapters 1-3 and says they all belong to volume 2."""

    async def list_chapters(self, external_id):
        return [SourceChapter(source_name=self.name, external_id=f"c{n}", number=float(n))
                for n in (1, 2, 3)]

    async def get_volume_map(self, external_id):
        return {1.0: 2, 2.0: 2, 3.0: 2}


async def _volume_series(session, root):
    """Chapters 1-3 owned via a volume-1 archive in an empty (unmounted) root."""
    archive = root / "Series" / "Series v01.cbz"
    series = Series(title="Series", sort_title="series", folder_name="Series",
                    root_folder=RootFolder(path=str(root)))
    series.source_links.append(SeriesSourceLink(source_name="fake", external_id="x"))
    for n in (1, 2, 3):
        series.chapters.append(Chapter(number=float(n), volume=1, downloaded=True,
                                       file_path=str(archive)))
    session.add(series)
    await session.commit()
    return series.id


async def _owned_state(session, series_id):
    series = await tasks._load_series(session, series_id)
    return sorted((c.number, c.volume, c.downloaded, c.file_path) for c in series.chapters)


@pytest.mark.parametrize("endpoint", [
    "scan", "resync_chapters", "resync_volumes", "resync_volumes_preview",
])
async def test_library_endpoints_409_while_root_is_unavailable(
    db_session, tmp_path, monkeypatch, endpoint
):
    from fastapi import HTTPException

    from mangarr.api import library as library_api

    root = tmp_path / "manga"
    root.mkdir()
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", VolumeSource())
    series_id = await _volume_series(db_session, root)
    before = await _owned_state(db_session, series_id)
    db_session.expunge_all()

    with pytest.raises(HTTPException) as err:
        await getattr(library_api, endpoint)(series_id, session=db_session)

    assert err.value.status_code == 409
    assert "empty" in err.value.detail
    db_session.expunge_all()
    assert await _owned_state(db_session, series_id) == before


async def test_scan_endpoint_409s_for_a_new_series_in_an_empty_root_others_own(
    db_session, tmp_path
):
    from fastapi import HTTPException

    from mangarr.api import library as library_api

    root = tmp_path / "manga"
    root.mkdir()
    shared = RootFolder(path=str(root))
    await _add_series(db_session, shared, "Owner", owned=3)
    newcomer = await _add_series(db_session, shared, "Newcomer")

    with pytest.raises(HTTPException) as err:
        await library_api.scan(newcomer.id, session=db_session)

    assert err.value.status_code == 409
