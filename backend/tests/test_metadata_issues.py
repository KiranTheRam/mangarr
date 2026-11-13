import os
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from mangarr.comicinfo import read_comicinfo, update_comicinfo
from mangarr.metadata_issues import detect_series_issues, find_candidates
from mangarr.api import issues as issues_api
from mangarr.models import Base, Chapter, LibraryIssue, RootFolder, Series

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def archive(path: Path, xml: str | None, pages: int = 1):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("pages/", b"")
        if xml is not None:
            zf.writestr("ComicInfo.xml", xml)
        for number in range(1, pages + 1):
            zf.writestr(f"{number:03}.png", PNG + bytes([number]))


def test_comicinfo_editor_preserves_pages_and_unknown_metadata(tmp_path):
    path = tmp_path / "chapter.cbz"
    archive(
        path,
        "<ComicInfo><Series>Old</Series><Number>92.1</Number><Volume>10</Volume>"
        "<Writer>Someone</Writer><PageCount>1</PageCount></ComicInfo>",
        pages=2,
    )
    os.chmod(path, 0o640)

    update_comicinfo(path, {"series": "Kagurabachi", "volume": None, "page_count": 2})

    info = read_comicinfo(path)
    assert info.series == "Kagurabachi"
    assert info.number == "92.1"
    assert info.volume is None
    assert info.page_count == "2" and info.actual_page_count == 2
    assert oct(path.stat().st_mode & 0o777) == "0o640"
    with zipfile.ZipFile(path) as zf:
        assert sorted(name for name in zf.namelist() if name.endswith(".png")) == [
            "001.png", "002.png"
        ]
        assert b"<Writer>Someone</Writer>" in zf.read("ComicInfo.xml")


def test_detects_isolated_volume_tag_and_recommends_removing_it(tmp_path):
    tagged = tmp_path / "Kagurabachi - Ch. 0092.1.cbz"
    plain = tmp_path / "Kagurabachi - Ch. 0092.cbz"
    archive(
        tagged,
        "<ComicInfo><Series>Kagurabachi</Series><Number>92.1</Number>"
        "<Volume>10</Volume><PageCount>1</PageCount></ComicInfo>",
    )
    archive(plain, None)
    series = Series(id=1, title="Kagurabachi", provider_title="Kagurabachi", alt_titles="")
    series.chapters = [
        Chapter(id=1, number=92.0, volume=10, downloaded=True, file_path=str(plain)),
        Chapter(
            id=2, number=92.1, volume=10, volume_source="disk-inferred",
            downloaded=True, file_path=str(tagged),
        ),
    ]

    issues = find_candidates(series)

    issue = next(item for item in issues if item.field == "volume")
    assert issue.kind == "comicinfo_volume_isolated"
    assert issue.current_value == "10" and issue.suggested_value is None
    assert "Kavita" in issue.detail


def test_detects_wrong_number_page_count_and_authoritative_volume(tmp_path):
    path = tmp_path / "Series Ch. 3.cbz"
    archive(
        path,
        "<ComicInfo><Series>Wrong Series</Series><Number>4</Number>"
        "<Volume>9</Volume><PageCount>99</PageCount></ComicInfo>",
        pages=2,
    )
    series = Series(id=1, title="Series", provider_title="Series", alt_titles="")
    series.chapters = [Chapter(
        id=3, number=3.0, volume=1, volume_source="viz",
        downloaded=True, file_path=str(path),
    )]

    issues = {item.field: item for item in find_candidates(series)}

    assert issues["number"].suggested_value == "3"
    assert issues["page_count"].suggested_value == "2"
    assert issues["series"].suggested_value == "Series"
    assert issues["volume"].suggested_value == "1"


@pytest.mark.asyncio
async def test_ignore_survives_redetection_and_resolved_issue_is_deleted(tmp_path):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    path = tmp_path / "Series Ch. 1.cbz"
    archive(path, "<ComicInfo><Series>Series</Series><Number>2</Number></ComicInfo>")
    async with maker() as session:
        series = Series(title="Series", provider_title="Series", alt_titles="")
        series.chapters = [Chapter(number=1.0, downloaded=True, file_path=str(path))]
        session.add(series)
        await session.commit()

        found = await detect_series_issues(session, series)
        found[0].ignored = True
        await session.commit()
        found_again = await detect_series_issues(session, series)
        assert found_again[0].id == found[0].id and found_again[0].ignored

        update_comicinfo(path, {"number": 1})
        await detect_series_issues(session, series)
        await session.commit()
        assert (await session.execute(select(LibraryIssue))).scalars().all() == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_api_accepts_recommendation_and_removes_resolved_issue(tmp_path, monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    series_dir = tmp_path / "Kagurabachi"
    series_dir.mkdir()
    plain = series_dir / "Kagurabachi - Ch. 0092.cbz"
    tagged = series_dir / "Kagurabachi - Ch. 0092.1.cbz"
    archive(plain, None)
    archive(tagged, "<ComicInfo><Series>Kagurabachi</Series><Number>92.1</Number>"
                    "<Volume>10</Volume><PageCount>1</PageCount></ComicInfo>")

    async def no_notify(*_args):
        return None

    monkeypatch.setattr(issues_api, "_notify_reader", no_notify)
    async with maker() as session:
        series = Series(
            title="Kagurabachi", provider_title="Kagurabachi", alt_titles="",
            folder_name="Kagurabachi", root_folder=RootFolder(path=str(tmp_path)),
        )
        series.chapters = [
            Chapter(number=92.0, volume=10, downloaded=True, file_path=str(plain)),
            Chapter(number=92.1, volume=10, volume_source="disk-inferred",
                    downloaded=True, file_path=str(tagged)),
        ]
        session.add(series)
        await session.commit()
        detected = await detect_series_issues(session, series)
        await session.commit()
        issue = next(item for item in detected if item.field == "volume")

        await issues_api.resolve_issue(issue.id, session)

        assert read_comicinfo(tagged).volume is None
        assert (await session.execute(select(LibraryIssue))).scalars().all() == []
    await engine.dispose()
