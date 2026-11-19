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
import os
from mangarr import metadata_issues
from mangarr.models import Chapter, Series
from pathlib import Path
from mangarr.api import library as library_api
from mangarr.models import Chapter, Download, DownloadKind, DownloadStatus, Series
from mangarr.sources import registry
from mangarr.metadata_issues import load_archive_ownership


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


async def test_resync_keeps_shared_archives_claimed_without_stalling_others(repair_db, monkeypatch):
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
        try:
            # The other series is not stalled behind the source fetch, but it
            # still sees the archive as shared and leaves it alone.
            await scan_and_repair()
            assert await issues_api.list_issues(ignored=False, series_id=series.id, session=session) == []
        finally:
            release.set()
            await resyncing

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


async def test_cancelled_resync_releases_its_claims_and_no_foreign_lock(repair_db, monkeypatch):
    session, maker, series, path = repair_db
    rebuilding = asyncio.Event()

    async def rebuild(*_args):
        rebuilding.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(tasks, "update_chapters", rebuild)
    archive_lock = library_api.manual_archive_lock
    await archive_lock.acquire()  # another operation is mid-write

    async def resync():
        async with maker() as db:
            await library_api.resync_chapters(series.id, db)

    async with asyncio.timeout(10):
        task = asyncio.create_task(resync())
        await rebuilding.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert archive_lock.locked(), "the cancelled resync released a lock it did not hold"
    archive_lock.release()
    from mangarr import metadata_issues
    assert metadata_issues._reserved_owners == {}


@pytest.mark.parametrize("shared_while_packing", [False, True])
async def test_volume_merge_packs_outside_the_archive_lock(repair_db, monkeypatch, shared_while_packing):
    from mangarr import metadata_issues

    session, _, series, path = repair_db
    second, later = path.with_name("Tracked Ch. 2.cbz"), path.with_name("Tracked Ch. 3.cbz")
    archive(second, "<ComicInfo><Number>2</Number></ComicInfo>")
    archive(later, "<ComicInfo><Number>3</Number></ComicInfo>")
    for number, volume, file_path in ((2, 1, second), (3, 2, later)):
        series.chapters.append(Chapter(number=number, volume=volume, downloaded=True, file_path=str(file_path)))
    for chapter in series.chapters:
        chapter.file_source = "alpha"
    await session.commit()
    await session.refresh(series, ["extra_folders"])
    pack = tasks.merge_chapter_archives
    lock_held_while_packing = []

    def packing(*args, **kwargs):
        lock_held_while_packing.append(metadata_issues.manual_archive_lock.locked())
        return pack(*args, **kwargs)

    monkeypatch.setattr(tasks, "merge_chapter_archives", packing)
    load = metadata_issues.load_archive_ownership
    calls = []

    async def ownership(db):
        current = await load(db)
        calls.append(current)
        if shared_while_packing and len(calls) == 2:
            current.owners[(second.stat().st_dev, second.stat().st_ino)].add(999)  # mapped by another series
        return current

    monkeypatch.setattr(metadata_issues, "load_archive_ownership", ownership)
    merged = await tasks.merge_complete_volumes(session, series, {}, force=True)

    assert lock_held_while_packing == [False]
    dest = path.with_name("Tracked - Vol. 01.cbz")
    if shared_while_packing:
        assert merged == [] and path.exists() and second.exists() and not dest.exists()
        assert {c.file_path for c in series.chapters if c.number in (1, 2)} == {str(path), str(second)}
    else:
        assert merged == [1] and dest.exists() and not path.exists()

# --- Reservations made during a re-sync must hold through renames, installs, and merges.


async def test_reserved_archive_stays_claimed_after_a_rename(repair_db, monkeypatch):
    session, maker, series, path = repair_db
    other = Series(
        title="Other", folder_name=series.folder_name, root_folder=series.root_folder,
        chapters=[Chapter(number=1, downloaded=True, file_path=str(path))],
    )
    session.add(other)
    await session.commit()
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

    renamed = path.with_name("Tracked - Ch. 001.cbz")
    async with asyncio.timeout(10):
        resyncing = asyncio.create_task(resync())
        await rebuilding.wait()
        try:
            # What rename_apply does for Tracked (series lock + archive lock).
            async with tasks.series_lock(series.id), metadata_issues.manual_archive_lock:
                os.rename(path, renamed)
                series.chapters[0].file_path = str(renamed)
                await session.commit()
            async with maker() as db:
                await library_api.scan(series.id, db)
                for issue in await issues_api.list_issues(ignored=False, series_id=series.id, session=db):
                    await issues_api.resolve_issue(issue.id, IssueResolveIn(revision=issue.revision), db)
        finally:
            release.set()
            await resyncing
    # Other re-adopted the renamed file after its rescan...
    async with maker() as db:
        from sqlalchemy import select
        rows = (await db.execute(select(Chapter.series_id, Chapter.file_path))).all()
    # ...but its ComicInfo was rewritten while Other's claim was in place.
    assert read_comicinfo(renamed).volume == "9"


async def test_direct_install_keeps_an_archive_a_resync_reserved(repair_db, monkeypatch):
    session, maker, series, path = repair_db
    src = next(iter(registry.DIRECT_SOURCES))
    chapter = series.chapters[0]
    chapter.file_source = src
    other = Series(
        title="Other", folder_name=series.folder_name, root_folder=series.root_folder,
        chapters=[Chapter(number=1, downloaded=True, file_path=str(path))],
    )
    session.add(other)
    dl = Download(series_id=series.id, chapter_id=chapter.id, kind=DownloadKind.DIRECT,
                  status=DownloadStatus.DOWNLOADING, source_name=src, title="x", payload="{}")
    session.add(dl)
    await session.commit()
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

    staged = path.with_name(".mangarr-direct-1.staged")
    archive(staged, "<ComicInfo><Number>1</Number></ComicInfo>")
    dest = path.with_name("Tracked Ch. 1 [new].cbz")

    async def ok():
        return None

    async with asyncio.timeout(10):
        resyncing = asyncio.create_task(resync())
        await rebuilding.wait()
        assert metadata_issues._reserved_owners  # Other still claims the archive
        try:
            async with tasks.series_lock(series.id), metadata_issues.manual_archive_lock:
                await tasks._install_direct_download(session, dl, series, chapter, staged, dest, ok)
        finally:
            release.set()
            await resyncing
    # Shared archive that Other reserved during its resync was deleted.
    assert path.exists(), "a reserved archive was discarded"


async def test_discarded_merge_keeps_a_volume_another_series_adopted(repair_db, monkeypatch):
    session, maker, series, path = repair_db
    second, later = path.with_name("Tracked Ch. 2.cbz"), path.with_name("Tracked Ch. 3.cbz")
    archive(second, "<ComicInfo><Number>2</Number></ComicInfo>")
    archive(later, "<ComicInfo><Number>3</Number></ComicInfo>")
    for number, volume, file_path in ((2, 1, second), (3, 2, later)):
        series.chapters.append(Chapter(number=number, volume=volume, downloaded=True, file_path=str(file_path)))
    for chapter in series.chapters:
        chapter.file_source = "alpha"
    await session.commit()
    await session.refresh(series, ["extra_folders"])
    dest = path.with_name("Tracked - Vol. 01.cbz")
    root_id = series.root_folder_id

    from mangarr import metadata_issues
    load = metadata_issues.load_archive_ownership
    calls = []

    async def ownership(db):
        calls.append(1)
        if len(calls) == 2:
            # Between packing and the lock, a series sharing the folder scans
            # and adopts chapter 2's file plus the new volume archive.
            assert dest.exists()
            async with maker() as s2:
                other = Series(title="Other", folder_name="Tracked", root_folder_id=root_id, chapters=[
                    Chapter(number=2, downloaded=True, file_path=str(second)),
                    Chapter(number=1, downloaded=True, file_path=str(dest)),
                ])
                s2.add(other)
                await s2.commit()
        return await load(db)

    monkeypatch.setattr(metadata_issues, "load_archive_ownership", ownership)
    merged = await tasks.merge_complete_volumes(session, series, {}, force=True)
    assert merged == []
    assert dest.exists(), "the adopted volume archive was deleted"


async def test_direct_upgrade_keeps_an_archive_another_series_maps_via_alias(repair_db):
    session, maker, series, path = repair_db
    src = next(iter(registry.DIRECT_SOURCES))
    chapter = series.chapters[0]
    chapter.file_source = src
    alias = path.with_name("Other Ch. 1.cbz")
    os.symlink(path, alias)
    other = Series(
        title="Other", folder_name=series.folder_name, root_folder=series.root_folder,
        chapters=[Chapter(number=1, downloaded=True, file_path=str(alias))],
    )
    session.add(other)
    dl = Download(series_id=series.id, chapter_id=chapter.id, kind=DownloadKind.DIRECT,
                  status=DownloadStatus.DOWNLOADING, source_name=src, title="x", payload="{}")
    session.add(dl)
    await session.commit()
    own = await load_archive_ownership(session)
    st = path.stat()
    print("owners per ArchiveOwnership:", own.owners[(st.st_dev, st.st_ino)])
    staged = path.with_name(".mangarr-direct-1.staged")
    archive(staged, "<ComicInfo><Number>1</Number></ComicInfo>")
    dest = path.with_name("Tracked Ch. 1 [new].cbz")

    async def ok():
        return None

    await tasks._install_direct_download(session, dl, series, chapter, staged, dest, ok)
    assert path.exists(), "archive Other maps through an alias was deleted (alias now dangles)"


async def test_reservations_never_mutate_a_snapshot_a_reader_holds(tmp_path):
    from mangarr import metadata_issues

    path = tmp_path / "Shared.cbz"
    archive(path, None)
    held = metadata_issues._reserved_owners
    before = dict(held)
    await metadata_issues.reserve_archive_owners(41, [str(path)])
    assert held == before  # worker threads iterate a mapping that never changes under them
    assert metadata_issues.ArchiveOwnership([]).owners[(path.stat().st_dev, path.stat().st_ino)] == {-41}
    claimed = metadata_issues._reserved_owners
    metadata_issues.release_archive_owners(41)
    assert set(claimed) == {41}
    assert metadata_issues._reserved_owners == {}


async def test_failed_merge_publish_removes_the_unreferenced_volume(repair_db, monkeypatch):
    from mangarr import metadata_issues

    session, _, series, path = repair_db
    second, later = path.with_name("Tracked Ch. 2.cbz"), path.with_name("Tracked Ch. 3.cbz")
    archive(second, "<ComicInfo><Number>2</Number></ComicInfo>")
    archive(later, "<ComicInfo><Number>3</Number></ComicInfo>")
    for number, volume, file_path in ((2, 1, second), (3, 2, later)):
        series.chapters.append(Chapter(number=number, volume=volume, downloaded=True, file_path=str(file_path)))
    for chapter in series.chapters:
        chapter.file_source = "alpha"
    await session.commit()
    await session.refresh(series, ["extra_folders"])
    load = metadata_issues.load_archive_ownership
    calls = []

    async def ownership(db):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("ownership snapshot failed")
        return await load(db)

    monkeypatch.setattr(metadata_issues, "load_archive_ownership", ownership)
    with pytest.raises(RuntimeError):
        await tasks.merge_complete_volumes(session, series, {}, force=True)

    assert not path.with_name("Tracked - Vol. 01.cbz").exists()
    assert path.exists() and second.exists()


async def test_cancelled_merge_keeps_a_volume_another_series_adopted(repair_db):
    from mangarr import metadata_issues
    session, maker, series, path = repair_db
    second = path.with_name("Tracked Ch. 2.cbz")
    later = path.with_name("Tracked Ch. 3.cbz")
    archive(second, "<ComicInfo><Number>2</Number></ComicInfo>")
    archive(later, "<ComicInfo><Number>3</Number></ComicInfo>")
    for number, volume, fp in ((2, 1, second), (3, 2, later)):
        series.chapters.append(Chapter(number=number, volume=volume, downloaded=True, file_path=str(fp)))
    for c in series.chapters:
        c.file_source = "alpha"
    await session.commit()
    await session.refresh(series, ["extra_folders"])
    dest = path.with_name("Tracked - Vol. 01.cbz")
    root_id = series.root_folder_id

    # Another series' scan holds the archive lock and adopts the new volume.
    await metadata_issues.manual_archive_lock.acquire()
    task = asyncio.create_task(tasks.merge_complete_volumes(session, series, {}, force=True))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if dest.exists():
            break
    assert dest.exists()
    async with maker() as s2:
        s2.add(Series(title="Other", folder_name="Tracked", root_folder_id=root_id, chapters=[
            Chapter(number=1, downloaded=True, file_path=str(dest)),
        ]))
        await s2.commit()
    await asyncio.sleep(0.05)
    task.cancel()  # e.g. shutdown
    with pytest.raises(asyncio.CancelledError):
        await task
    metadata_issues.manual_archive_lock.release()
    assert dest.exists(), "volume archive another series maps was deleted"
