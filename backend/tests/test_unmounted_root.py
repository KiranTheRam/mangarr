"""A library root that is missing or unmounted must not read as an empty
library: that cleared every chapter and the monitor re-downloaded it all."""

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
