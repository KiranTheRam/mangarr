"""A library root that goes away while a direct download is running must not
be written to, and must not count against the source: the download passed the
worker's library check, then the share dropped while the pages were fetched."""

import errno
import os
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.download import direct
from mangarr.jobs import tasks
from mangarr.models import (
    Base,
    Chapter,
    Download,
    DownloadStatus,
    RootFolder,
    Series,
    SeriesSourceLink,
)
from mangarr.sources.base import DirectSource, SourceChapter

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class PagedSource(DirectSource):
    """Serves chapter 2 as two pages; on_page runs as each page is fetched."""

    name = "fake"

    def __init__(self, on_page=lambda: None):
        self.on_page = on_page

    async def search_series(self, query):
        return []

    async def list_chapters(self, external_id):
        return [SourceChapter(source_name=self.name, external_id="c2", number=2.0)]

    async def get_pages(self, chapter_external_id):
        return ["http://pages/1", "http://pages/2"]

    async def download_page(self, client, url):
        self.on_page()
        return PNG


@pytest.fixture
async def db_session(monkeypatch, tmp_path):
    from mangarr import settings_service
    monkeypatch.setitem(settings_service.DEFAULTS, "source_fake_enabled", "true")
    # file-backed: a failing page fetch cancels the others mid-query, and the
    # session then continues on a new connection, which must see the same data
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mangarr.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _put_chapter_1(root: Path) -> Path:
    path = root / "Series" / "Series - Ch. 0001.cbz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"cbz")
    return path


async def _queue_chapter_2(session, monkeypatch, root: Path, source: PagedSource):
    """A mounted series owning chapter 1 whose chapter 2 the monitor queued
    from `source`; returns (values, queued download)."""
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", source)
    chapter_1 = _put_chapter_1(root)
    series = Series(title="Series", sort_title="series", folder_name="Series",
                    root_folder=RootFolder(path=str(root)))
    series.source_links.append(SeriesSourceLink(source_name="fake", external_id="x"))
    series.chapters.append(Chapter(number=1.0, monitored=True, downloaded=True,
                                   file_path=str(chapter_1)))
    series.chapters.append(Chapter(number=2.0, monitored=True))
    session.add(series)
    await session.commit()
    values = await tasks.registry.apply_settings(session)
    series = await tasks._load_series(session, series.id)
    assert await tasks.grab_missing_chapters(session, series, values) == 1
    dl = (await session.execute(select(Download))).scalar_one()
    assert (dl.status, dl.source_name) == (DownloadStatus.QUEUED, "fake")
    return values, dl


async def _grab_again(session, dl, values) -> list[Download]:
    """Run the next monitor pass; return the downloads it queued."""
    series = await tasks._load_series(session, dl.series_id)
    await tasks.grab_missing_chapters(session, series, values)
    return list((await session.execute(
        select(Download).where(Download.id != dl.id)
    )).scalars())


@pytest.mark.parametrize("lost_as", ["emptied", "removed"])
async def test_library_lost_while_pages_download_is_not_written_or_blamed(
    db_session, tmp_path, monkeypatch, lost_as
):
    # "emptied": the share unmounts and leaves its empty mount point behind;
    # "removed": the root is a folder on the share, so the path is gone
    root = tmp_path / "share" / "manga"

    def drop_share():
        if lost_as == "emptied" and (root / "Series").exists():
            shutil.rmtree(root / "Series")
        if lost_as == "removed" and root.exists():
            shutil.rmtree(root)

    values, dl = await _queue_chapter_2(db_session, monkeypatch, root, PagedSource(drop_share))

    await tasks._run_direct_download(db_session, dl)
    await db_session.refresh(dl)
    chapter = await db_session.get(Chapter, dl.chapter_id)
    await db_session.refresh(chapter)

    assert dl.status == DownloadStatus.FAILED
    assert dl.error.startswith(tasks.LIBRARY_UNAVAILABLE)
    assert not chapter.downloaded
    # nothing was written onto the bare mount point
    if lost_as == "emptied":
        assert list(root.iterdir()) == []
    else:
        assert not root.exists()

    # the share is back: the chapter comes from the same source again
    _put_chapter_1(root)
    retry = await _grab_again(db_session, dl, values)
    assert [(r.chapter_id, r.source_name, r.status) for r in retry] == [
        (dl.chapter_id, "fake", DownloadStatus.QUEUED)
    ]


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits don't bind root")
async def test_library_lost_during_the_write_is_not_blamed(
    db_session, tmp_path, monkeypatch
):
    # the share fails while the archive is being written: the write raises an
    # I/O error and the root can no longer be listed
    root = tmp_path / "manga"
    values, dl = await _queue_chapter_2(db_session, monkeypatch, root, PagedSource())
    partial = root / "Series" / "Series - Ch. 0002.cbz.partial"

    def failing_write(dest, pages, comicinfo_xml):
        dest.with_suffix(".cbz.partial").write_bytes(b"half an archive")
        os.chmod(root, 0o300)
        raise OSError(errno.EIO, "Input/output error", str(dest))

    monkeypatch.setattr(direct, "write_cbz", failing_write)
    try:
        await tasks._run_direct_download(db_session, dl)
    finally:
        os.chmod(root, 0o755)
    await db_session.refresh(dl)

    assert dl.status == DownloadStatus.FAILED
    assert dl.error.startswith(tasks.LIBRARY_UNAVAILABLE)
    assert "can't be read" in dl.error
    assert not partial.exists()
    retry = await _grab_again(db_session, dl, values)
    assert [(r.source_name, r.status) for r in retry] == [("fake", DownloadStatus.QUEUED)]


async def test_a_write_error_with_the_library_present_still_blocks(
    db_session, tmp_path, monkeypatch
):
    # the library is mounted and readable: a failing write isn't explained by
    # an unavailable library and keeps its error and its block
    root = tmp_path / "manga"
    values, dl = await _queue_chapter_2(db_session, monkeypatch, root, PagedSource())

    def failing_write(dest, pages, comicinfo_xml):
        raise PermissionError(errno.EACCES, "Permission denied", str(dest))

    monkeypatch.setattr(direct, "write_cbz", failing_write)
    await tasks._run_direct_download(db_session, dl)
    await db_session.refresh(dl)

    assert dl.status == DownloadStatus.FAILED
    assert dl.error.startswith("[Errno 13] Permission denied")
    assert await _grab_again(db_session, dl, values) == []


async def test_a_source_connection_error_still_blocks(
    db_session, tmp_path, monkeypatch
):
    # ConnectionError is an OSError too; with the library fine it is still
    # the source's failure
    root = tmp_path / "manga"

    class Refusing(PagedSource):
        async def get_pages(self, chapter_external_id):
            raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

    values, dl = await _queue_chapter_2(db_session, monkeypatch, root, Refusing())
    await tasks._run_direct_download(db_session, dl)
    await db_session.refresh(dl)

    assert dl.status == DownloadStatus.FAILED
    assert not dl.error.startswith(tasks.LIBRARY_UNAVAILABLE)
    assert await _grab_again(db_session, dl, values) == []


async def test_a_download_with_the_library_present_is_written(
    db_session, tmp_path, monkeypatch
):
    root = tmp_path / "manga"
    values, dl = await _queue_chapter_2(db_session, monkeypatch, root, PagedSource())

    await tasks._run_direct_download(db_session, dl)
    await db_session.refresh(dl)
    chapter = await db_session.get(Chapter, dl.chapter_id)
    await db_session.refresh(chapter)

    assert (dl.status, dl.error) == (DownloadStatus.DONE, "")
    assert chapter.downloaded
    assert Path(chapter.file_path).parent == root / "Series"
    assert Path(chapter.file_path).is_file()
    assert not Path(chapter.file_path).with_suffix(".cbz.partial").exists()
