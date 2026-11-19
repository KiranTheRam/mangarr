"""Shared or uncertain archives must not be judged or rewritten as one series' own."""

import asyncio

import pytest

from mangarr.api import issues as issues_api, library as library_api
from mangarr.comicinfo import read_comicinfo
from mangarr.jobs import tasks
from mangarr.library.scanner import scan_series
from mangarr.metadata_issues import ArchiveOwnership, find_candidates
from mangarr.models import Chapter, RootFolder, Series
from mangarr.schemas import IssueResolveIn
from tests.test_metadata_issues import archive, repair_db  # noqa: F401


def _volume_series(tmp_path, chapters):
    series = Series(id=1, title="Kagurabachi", provider_title="Kagurabachi", alt_titles="")
    series.chapters = chapters
    series.root_folder = RootFolder(path=str(tmp_path.parent))
    series.folder_name = tmp_path.name
    return series


@pytest.mark.parametrize("sibling", ["shared", "disputed", "unreadable", "untagged"])
def test_isolated_volume_requires_every_sibling_to_be_known(tmp_path, sibling):
    tagged = tmp_path / "Kagurabachi - Ch. 0001.cbz"
    other = tmp_path / ("Kagurabachi - Ch. 0004.cbz" if sibling == "disputed" else "Kagurabachi - Ch. 0002.cbz")
    plain = tmp_path / "Kagurabachi - Ch. 0003.cbz"
    archive(tagged, "<ComicInfo><Number>1</Number><Volume>1</Volume></ComicInfo>")
    if sibling == "unreadable":
        other.write_bytes(b"not a zip")
    else:
        volume = "" if sibling == "untagged" else "<Volume>1</Volume>"
        archive(other, f"<ComicInfo><Number>2</Number>{volume}</ComicInfo>")
    archive(plain, "<ComicInfo><Number>3</Number></ComicInfo>")
    chapters = [
        Chapter(id=n, number=float(n), volume=1, volume_source="disk-inferred", downloaded=True, file_path=str(p))
        for n, p in ((1, tagged), (2, other), (3, plain))
    ] + [Chapter(id=4, number=4.0, volume=2, volume_source="disk-inferred")]
    paths = [(c.id, c.file_path) for c in chapters if c.file_path]
    if sibling == "shared":
        paths.append((99, str(other)))  # a chapter of another series maps it too

    isolated = [
        item for item in find_candidates(_volume_series(tmp_path, chapters), ArchiveOwnership(paths))
        if item.kind == "comicinfo_volume_isolated"
    ]

    assert [item.chapter_id for item in isolated] == ([1] if sibling == "untagged" else [])


async def test_resync_holds_shared_archives_until_mappings_return(repair_db, monkeypatch):
    session, maker, series, path = repair_db
    other = Series(
        title="Other", folder_name=series.folder_name, root_folder=series.root_folder,
        chapters=[Chapter(number=1, downloaded=True, file_path=str(path))],
    )
    session.add(other)
    await session.commit()
    await issues_api.scan_issues(session)
    assert await issues_api.list_issues(ignored=False, series_id=series.id, session=session) == []
    rebuilding, release = asyncio.Event(), asyncio.Event()

    async def rebuild(rebuild_session, rebuilding_series, _values):
        rebuilding.set()
        await release.wait()
        rebuilding_series.chapters = [Chapter(number=1)]
        await rebuild_session.commit()

    monkeypatch.setattr(tasks, "update_chapters", rebuild)

    async def resync():
        async with maker() as db:
            await library_api.resync_chapters(other.id, db)

    async def scan_and_repair():
        async with maker() as db:
            await library_api.scan(series.id, db)
            for issue in await issues_api.list_issues(ignored=False, series_id=series.id, session=db):
                await issues_api.resolve_issue(issue.id, IssueResolveIn(revision=issue.revision), db)

    async with asyncio.timeout(10):
        resyncing = asyncio.create_task(resync())
        await rebuilding.wait()
        repairing = asyncio.create_task(scan_and_repair())
        try:
            done, _ = await asyncio.wait({repairing}, timeout=0.05)
            assert not done, "another series scanned the archive while its co-owner was rebuilding"
        finally:
            release.set()
            await asyncio.gather(resyncing, repairing)

    assert read_comicinfo(path).volume == "9"


def test_scan_adopts_a_chapter_symlink_that_points_outside_the_folder(tmp_path):
    folder = tmp_path / "library" / "Series"
    folder.mkdir(parents=True)
    seed = tmp_path / "downloads" / "Series Ch. 1.cbz"
    seed.parent.mkdir()
    archive(seed, None)
    (folder / "Series Ch. 1.cbz").symlink_to(seed)
    chapter = Chapter(number=1.0)
    series = Series(title="Series", folder_name="Series", alt_titles="")
    series.chapters = [chapter]

    scan_series(series, [chapter], [folder], {}, root=tmp_path / "library", expect_content=True)

    assert chapter.downloaded


async def test_volume_merge_skips_a_volume_another_series_maps(repair_db):
    session, _, series, path = repair_db
    second, later = path.with_name("Tracked Ch. 2.cbz"), path.with_name("Tracked Ch. 3.cbz")
    archive(second, "<ComicInfo><Number>2</Number></ComicInfo>")
    archive(later, "<ComicInfo><Number>3</Number></ComicInfo>")
    for number, volume, file_path in ((2, 1, second), (3, 2, later)):
        series.chapters.append(Chapter(number=number, volume=volume, downloaded=True, file_path=str(file_path)))
    for chapter in series.chapters:
        chapter.file_source = "alpha"
    session.add(Series(
        title="Other", folder_name="Other", root_folder=series.root_folder,
        chapters=[Chapter(number=7, downloaded=True, file_path=str(second))],
    ))
    await session.commit()
    await session.refresh(series, ["extra_folders"])

    assert await tasks.merge_complete_volumes(session, series, {}, force=True) == []
    assert path.exists() and second.exists()
