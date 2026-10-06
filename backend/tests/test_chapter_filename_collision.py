"""Distinct chapter numbers must never share a library file: two-decimal
chapters (12.21, 12.24 …) are real and some sources return them."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.jobs import tasks
from mangarr.library.naming import (
    DEFAULT_TEMPLATE,
    DEFAULT_TEMPLATE_NO_VOLUME,
    chapter_filename,
)
from mangarr.models import (
    Base,
    Chapter,
    Download,
    DownloadKind,
    DownloadStatus,
    RootFolder,
    Series,
)
from mangarr.sources.base import DirectSource
from mangarr.util import parse_chapter_number


def name(chapter: float) -> str:
    return chapter_filename(DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME, "Series", chapter)


@pytest.mark.parametrize("number, expected", [
    (12.21, "Series - Ch. 0012.21.cbz"),
    (12.24, "Series - Ch. 0012.24.cbz"),
    (12.25, "Series - Ch. 0012.25.cbz"),
    (12.55, "Series - Ch. 0012.55.cbz"),
    (21.5, "Series - Ch. 0021.5.cbz"),
    (0.5, "Series - Ch. 0000.5.cbz"),
    (1100.125, "Series - Ch. 1100.125.cbz"),
    (21, "Series - Ch. 0021.cbz"),
])
def test_default_name_keeps_every_decimal(number, expected):
    assert name(number) == expected


def test_two_decimal_chapters_get_distinct_names_that_parse_back():
    numbers = [12.2, 12.21, 12.24, 12.25, 12.5, 12.55]
    names = [name(n) for n in numbers]
    assert len(set(names)) == len(numbers)
    assert [parse_chapter_number(n.removesuffix(".cbz")) for n in names] == numbers


class FakeSource(DirectSource):
    name = "fake"

    async def search_series(self, query):
        return []

    async def list_chapters(self, external_id):
        return []

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


async def test_download_refuses_to_overwrite_another_chapters_file(
    db_session, tmp_path, monkeypatch
):
    from mangarr import settings_service
    # a custom template that drops the decimals maps 12.21 and 12.24 to one file
    lossy = "{series} - Ch. {chapter:04.0f}"
    monkeypatch.setitem(settings_service.DEFAULTS, "naming_template", lossy)
    monkeypatch.setitem(settings_service.DEFAULTS, "naming_template_no_volume", lossy)
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())

    existing = tmp_path / "Series" / "Series - Ch. 0012.cbz"
    existing.parent.mkdir()
    existing.write_bytes(b"chapter 12.21")
    series = Series(title="Series", sort_title="series",
                    root_folder=RootFolder(path=str(tmp_path)), folder_name="Series")
    owner = Chapter(number=12.21, downloaded=True, file_path=str(existing))
    target = Chapter(number=12.24, monitored=True)
    series.chapters.extend([owner, target])
    db_session.add(series)
    await db_session.commit()
    dl = Download(series_id=series.id, chapter_id=target.id, kind=DownloadKind.DIRECT,
                  status=DownloadStatus.QUEUED, source_name="fake", payload="c",
                  title="Series - Chapter 12.24")
    db_session.add(dl)
    await db_session.commit()

    written = []

    async def fake_download(source, payload, series, chapter, dest,
                            progress_cb=None, cancel_cb=None, web_url=""):
        written.append(dest)
        dest.write_bytes(b"chapter 12.24")

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", fake_download)

    await tasks._run_direct_download(db_session, dl)
    await db_session.refresh(dl)
    await db_session.refresh(target)

    assert written == []
    assert existing.read_bytes() == b"chapter 12.21"
    assert dl.status == DownloadStatus.FAILED
    assert "already holds chapter 12.21" in dl.error
    assert not target.downloaded
