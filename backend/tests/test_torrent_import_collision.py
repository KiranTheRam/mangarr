"""A torrent import must not record a chapter on another chapter's file: an
existing destination is adopted as the chapter's own, so a naming template
that maps two chapters to one name would drop the imported chapter's pages."""

import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr import settings_service
from mangarr.jobs import tasks
from mangarr.library.naming import DEFAULT_TEMPLATE, chapter_filename
from mangarr.models import (
    Base,
    Chapter,
    Download,
    DownloadKind,
    DownloadStatus,
    RootFolder,
    Series,
)

# accepted by settings validation; renders 12 and 12.5 as "c012"
LOSSY = "{series} - c{chapter:03.0f}"


def cbz(path: Path, tag: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.png", b"\x89PNG\r\n\x1a\n" + tag * 64)
        archive.writestr("002.png", b"\x89PNG\r\n\x1a\n" + tag * 65)


def tags(path: Path) -> set[bytes]:
    with zipfile.ZipFile(path) as archive:
        return {archive.read(name)[8:9] for name in archive.namelist()}


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def import_payload(session, tmp_path, chapters, payload, template=LOSSY):
    await settings_service.set_many(session, {
        "naming_template": template, "naming_template_no_volume": template,
    })
    values = await tasks.registry.apply_settings(session)
    (tmp_path / "manga").mkdir(exist_ok=True)
    series = Series(title="Test Series", sort_title="test series",
                    root_folder=RootFolder(path=str(tmp_path / "manga")),
                    folder_name="Test Series")
    series.chapters.extend(chapters)
    session.add(series)
    await session.commit()
    dl = Download(series_id=series.id, kind=DownloadKind.TORRENT,
                  status=DownloadStatus.IMPORTING, title="pack", source_name="nyaa",
                  payload="magnet:", torrent_hash="a" * 40)
    session.add(dl)
    await session.commit()
    dl_id = dl.id
    await tasks._import_torrent(session, dl, payload, values)
    session.expire_all()
    rows = (await session.execute(select(Chapter).order_by(Chapter.number))).scalars().all()
    return await session.get(Download, dl_id), {c.number: c for c in rows}


async def test_two_chapters_of_one_torrent_never_share_a_file(db_session, tmp_path):
    payload = tmp_path / "qbt" / "Test Series c012-012.5"
    cbz(payload / "Test Series - Chapter 012.cbz", b"A")
    cbz(payload / "Test Series - Chapter 012.5.cbz", b"B")

    dl, chapters = await import_payload(
        db_session, tmp_path,
        [Chapter(number=12.0, monitored=True), Chapter(number=12.5, monitored=True)],
        payload,
    )

    assert dl.status == DownloadStatus.FAILED
    assert "would both be saved as Test Series - c012.cbz" in dl.error
    assert "naming template" in dl.error
    assert not chapters[12.0].downloaded and chapters[12.0].file_path == ""
    assert not chapters[12.5].downloaded and chapters[12.5].file_path == ""
    # refused before anything was placed: no file holds either chapter's pages
    assert list((tmp_path / "manga" / "Test Series").iterdir()) == []
    assert tags(payload / "Test Series - Chapter 012.cbz") == {b"A"}
    assert tags(payload / "Test Series - Chapter 012.5.cbz") == {b"B"}


async def test_torrent_chapter_is_not_recorded_on_another_chapters_file(db_session, tmp_path):
    existing = tmp_path / "manga" / "Test Series" / "Test Series - c012.cbz"
    cbz(existing, b"A")
    payload = tmp_path / "qbt" / "Test Series - Chapter 012.5.cbz"
    cbz(payload, b"B")

    dl, chapters = await import_payload(
        db_session, tmp_path,
        [Chapter(number=12.0, monitored=True, downloaded=True,
                 file_path=str(existing), file_source="mangadex"),
         Chapter(number=12.5, monitored=True)],
        payload,
    )

    assert dl.status == DownloadStatus.FAILED
    assert "Test Series - c012.cbz already holds chapter 12;" in dl.error
    assert not chapters[12.5].downloaded and chapters[12.5].file_path == ""
    assert chapters[12.0].downloaded
    assert (chapters[12.0].file_path, chapters[12.0].file_source) == (str(existing), "mangadex")
    assert tags(existing) == {b"A"}
    assert sorted(p.name for p in existing.parent.iterdir()) == ["Test Series - c012.cbz"]


async def test_reimporting_a_chapter_onto_its_own_file_still_adopts_it(db_session, tmp_path):
    existing = tmp_path / "manga" / "Test Series" / "Test Series - c012.cbz"
    cbz(existing, b"A")
    payload = tmp_path / "qbt" / "Test Series - Chapter 012.cbz"
    cbz(payload, b"C")

    dl, chapters = await import_payload(
        db_session, tmp_path,
        [Chapter(number=12.0, monitored=True, downloaded=True,
                 file_path=str(existing), file_source="mangadex"),
         Chapter(number=13.0, monitored=True)],
        payload,
    )

    assert (dl.status, dl.error) == (DownloadStatus.DONE, "")
    assert chapters[12.0].downloaded
    assert (chapters[12.0].file_path, chapters[12.0].file_source) == (str(existing), "mangadex")
    assert tags(existing) == {b"A"}


async def test_chapters_with_distinct_names_still_import_together(db_session, tmp_path):
    payload = tmp_path / "qbt" / "Test Series c012-013"
    cbz(payload / "Test Series - Chapter 012.cbz", b"A")
    cbz(payload / "Test Series - Chapter 012.5.cbz", b"B")
    cbz(payload / "Test Series - Chapter 013.cbz", b"C")

    dl, chapters = await import_payload(
        db_session, tmp_path,
        [Chapter(number=n, monitored=True) for n in (12.0, 12.5, 13.0)],
        payload, template=DEFAULT_TEMPLATE,
    )

    assert (dl.status, dl.error) == (DownloadStatus.DONE, "")
    folder = tmp_path / "manga" / "Test Series"
    for number, tag in ((12.0, b"A"), (12.5, b"B"), (13.0, b"C")):
        chapter = chapters[number]
        name = chapter_filename(DEFAULT_TEMPLATE, DEFAULT_TEMPLATE, "Test Series", number)
        assert chapter.downloaded and chapter.file_source == "nyaa"
        assert chapter.file_path == str(folder / name)
        assert tags(folder / name) == {tag}
    assert len(list(folder.iterdir())) == 3
