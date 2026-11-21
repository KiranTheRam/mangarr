import asyncio
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.api import issues
from mangarr.comicinfo import COMICINFO_INT_MAX
from mangarr.jobs import tasks
from mangarr.metadata_issues import ArchiveOwnership, find_candidates
from mangarr.models import Base, Chapter, RootFolder, Series
from mangarr.schemas import ComicInfoUpdateIn


def make_archive(path: Path, fields: str) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr("001.png", b"page")
        archive.writestr("ComicInfo.xml", f"<ComicInfo>{fields}</ComicInfo>")


@pytest.mark.parametrize("volume", ["", "<Volume> </Volume>"])
async def test_setting_volume_cannot_lock_an_unnumbered_archive_out_of_the_editor(tmp_path, monkeypatch, volume):
    monkeypatch.setattr(tasks, "_SERIES_LOCKS", {})
    monkeypatch.setattr(issues, "manual_archive_lock", asyncio.Lock())
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        folder = tmp_path / "Tracked"
        folder.mkdir()
        path = folder / "Bonus Story.cbz"
        make_archive(path, f"<Title>Bonus</Title>{volume}")
        original = path.read_bytes()

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            chapter = Chapter(number=1, volume=1, downloaded=True, file_path=str(path))
            series = Series(
                title="Tracked", root_folder=RootFolder(path=str(tmp_path)), chapters=[chapter],
            )
            session.add(series)
            await session.commit()

            before = await issues.get_chapter_comicinfo(series.id, chapter.id, session)
            assert before.volume == ("" if volume else None)
            with pytest.raises(HTTPException, match="Set ComicInfo Number to chapter 1") as exc:
                await issues.update_chapter_comicinfo(
                    series.id, chapter.id, ComicInfoUpdateIn(revision=before.revision, volume=1), session,
                )
            assert exc.value.status_code == 409
            assert path.read_bytes() == original
            assert await issues.get_chapter_comicinfo(series.id, chapter.id, session) == before

            edited = await issues.update_chapter_comicinfo(
                series.id, chapter.id, ComicInfoUpdateIn(revision=before.revision, number="1", volume=1),
                session,
            )
            assert edited.number == edited.volume == "1"
            assert await issues.get_chapter_comicinfo(series.id, chapter.id, session) == edited
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("volume", "page_count", "expected"),
    [
        ("1.0", "1e0", {"volume", "page_count"}),
        ("1e0", "1.0", {"volume", "page_count"}),
        ("2147483648", "2147483648", {"volume", "page_count"}),
        ("\u00a01", "\u0661", {"volume", "page_count"}),
        ("\t ", " \n", {"volume", "page_count"}),
        ("", "", set()),
        ("\t +0001\n", " +001 ", set()),
    ],
)
def test_volume_and_page_count_require_xml_schema_integers(tmp_path, volume, page_count, expected):
    folder = tmp_path / "Tracked"
    folder.mkdir()
    path = folder / "Tracked Ch. 1.cbz"
    make_archive(path, f"<Number>1</Number><Volume>{volume}</Volume><PageCount>{page_count}</PageCount>")
    chapter = Chapter(
        id=1, number=1, volume=1, volume_source="viz", downloaded=True, file_path=str(path),
    )
    series = Series(
        id=1, title="Tracked", folder_name=folder.name, root_folder=RootFolder(path=str(tmp_path)),
        chapters=[chapter],
    )

    candidates = find_candidates(series, ArchiveOwnership([(chapter.id, str(path))]))
    assert candidates is not None
    assert {candidate.field for candidate in candidates} == expected
    for candidate in candidates:
        if candidate.field == "page_count":
            assert candidate.suggested_value == "1"
        else:
            assert candidate.kind == "comicinfo_volume_invalid"


@pytest.mark.parametrize("field", ["volume", "page_count"])
def test_manual_integer_update_cannot_write_values_outside_comicinfo_schema(field):
    assert getattr(ComicInfoUpdateIn(revision="revision", **{field: COMICINFO_INT_MAX}), field) == COMICINFO_INT_MAX
    with pytest.raises(ValidationError, match="must be at most"):
        ComicInfoUpdateIn(revision="revision", **{field: COMICINFO_INT_MAX + 1})
