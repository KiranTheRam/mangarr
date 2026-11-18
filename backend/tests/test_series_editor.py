"""The library mass editor: bulk monitoring, moving series to another root
folder, bulk refresh/search and bulk delete."""

import asyncio
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from mangarr.api import issues as issues_api, library as library_api, series as series_api
from mangarr.comicinfo import read_comicinfo
from mangarr.models import (
    Base,
    Chapter,
    Download,
    DownloadKind,
    DownloadStatus,
    RootFolder,
    Series,
    SeriesFolder,
)
from mangarr.schemas import IssueResolveIn, RenameApplyIn, SeriesBulkIn, SeriesBulkRefreshIn, SeriesEditorIn


@pytest.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def roots(tmp_path: Path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    return old, new


async def _add(session, title: str, root: RootFolder, numbers=(1, 2, 3), **kw) -> Series:
    series = Series(title=title, sort_title=title, folder_name=title, root_folder=root, **kw)
    series.chapters = [Chapter(number=n, monitored=True) for n in numbers]
    session.add(series)
    await session.commit()
    return series


async def _edit(session, **kw):
    return await series_api.edit_series_bulk(SeriesEditorIn(**kw), session)


async def test_bulk_monitoring_applies_the_mode_to_every_series(session, roots):
    root = RootFolder(path=str(roots[0]))
    a = await _add(session, "Alpha", root)
    b = await _add(session, "Beta", root, monitored=False)

    out = await _edit(session, series_ids=[a.id, b.id], monitored=True, monitor_mode="none")

    assert out.updated == 2 and not out.problems
    for s in (a, b):
        assert s.monitored and s.monitor_mode == "none"
        assert not any(c.monitored for c in s.chapters)


async def test_bulk_from_chapter_needs_a_threshold(session, roots):
    a = await _add(session, "Alpha", RootFolder(path=str(roots[0])))
    with pytest.raises(HTTPException) as err:
        await _edit(session, series_ids=[a.id], monitor_mode="from_chapter")
    assert err.value.status_code == 422

    await _edit(session, series_ids=[a.id], monitor_mode="from_chapter", monitor_from=2)
    assert [c.monitored for c in a.chapters] == [False, True, True]


async def test_root_change_without_move_keeps_files_and_extra_folders_in_place(session, roots):
    old, new = roots
    old_root, new_root = RootFolder(path=str(old)), RootFolder(path=str(new))
    session.add(new_root)
    a = await _add(session, "Alpha", old_root, extra_folders=[SeriesFolder(path="Alpha Volumes")])

    out = await _edit(session, series_ids=[a.id], root_folder_id=new_root.id)

    assert out.updated == 1 and out.moved == 0
    assert a.root_folder_id == new_root.id
    # the relative extra folder still means the directory in the old root
    assert a.extra_folders[0].path == str(old / "Alpha Volumes")


async def test_move_carries_the_folder_and_repoints_chapters(session, roots):
    old, new = roots
    old_root, new_root = RootFolder(path=str(old)), RootFolder(path=str(new))
    session.add(new_root)
    a = await _add(session, "Alpha", old_root, extra_folders=[
        SeriesFolder(path="Alpha/Volumes"), SeriesFolder(path="Loose"),
    ])
    (old / "Alpha" / "Volumes").mkdir(parents=True)
    (old / "Alpha" / "Ch 1.cbz").write_bytes(b"x")
    (old / "Loose").mkdir()
    ch = a.chapters[0]
    ch.downloaded = True
    ch.file_path = str(old / "Alpha" / "Ch 1.cbz")
    ch.file_source, ch.file_group = "mangadex", "Some Group"
    await session.commit()

    out = await _edit(session, series_ids=[a.id], root_folder_id=new_root.id, move_files=True)

    assert out.moved == 1 and not out.problems
    assert (new / "Alpha" / "Ch 1.cbz").is_file() and not (old / "Alpha").exists()
    assert ch.file_path == str(new / "Alpha" / "Ch 1.cbz")
    # the same file, so its provenance survives the path change
    assert (ch.file_source, ch.file_group) == ("mangadex", "Some Group")
    assert sorted(f.path for f in a.extra_folders) == [
        str(new / "Alpha" / "Volumes"), str(old / "Loose"),
    ]
    assert a.root_folder_id == new_root.id


@pytest.mark.parametrize("extra_available", [False, True])
async def test_move_carries_ignored_archive_warning_to_new_root(session, roots, extra_available):
    old, new = roots
    old_root, new_root = RootFolder(path=str(old)), RootFolder(path=str(new))
    session.add(new_root)
    series = await _add(
        session, "Alpha", old_root, numbers=(1, 2),
        extra_folders=[SeriesFolder(path="Outside")],
    )
    (old / "Alpha").mkdir()
    (old / "Outside").mkdir()
    primary = old / "Alpha" / "Ch 1.cbz"
    outside = old / "Outside" / "Ch 2.cbz"
    for path in (primary, outside):
        with ZipFile(path, "w") as archive:
            archive.writestr("001.jpg", b"page")
            archive.writestr("ComicInfo.xml", "<ComicInfo><PageCount>2</PageCount></ComicInfo>")
    await library_api.scan(series.id, session)
    warnings = {
        warning.file_path: warning
        for warning in await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    }
    await issues_api.ignore_issue(warnings[str(primary)].id, session)
    if not extra_available:
        outside.parent.rename(old / "Detached")

    out = await _edit(session, series_ids=[series.id], root_folder_id=new_root.id, move_files=True)

    assert out.moved == 1 and not out.problems
    moved_path = new / "Alpha" / "Ch 1.cbz"
    [moved] = await issues_api.list_issues(ignored=True, series_id=series.id, session=session)
    assert moved.id == warnings[str(primary)].id
    assert moved.file_path == str(moved_path)
    assert (moved.revision != warnings[str(primary)].revision) is extra_available
    assert series.extra_folders[0].path == str(outside.parent)
    assert await issues_api.list_issues(ignored=False, series_id=series.id, session=session) == [
        warnings[str(outside)],
    ]

    if not extra_available:
        (old / "Detached").rename(outside.parent)
    await library_api.scan(series.id, session)
    [rescanned] = await issues_api.list_issues(ignored=True, series_id=series.id, session=session)
    assert rescanned.id == moved.id and rescanned.file_path == moved.file_path
    assert rescanned.revision != warnings[str(primary)].revision
    await issues_api.restore_issue(rescanned.id, session)
    await issues_api.resolve_issue(rescanned.id, IssueResolveIn(revision=rescanned.revision), session)
    assert read_comicinfo(moved_path).page_count == "1"


async def test_move_reloads_paths_after_a_concurrent_rename(tmp_path, roots, monkeypatch):
    old, new = roots
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'concurrent-move.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    entered = asyncio.Event()
    renamed = asyncio.Event()
    original_get_settings = series_api.settings_service.get_all

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as setup:
            new_root = RootFolder(path=str(new))
            setup.add(new_root)
            series = await _add(setup, "Alpha", RootFolder(path=str(old)), numbers=(1,))
            series_id, new_root_id = series.id, new_root.id
            library = old / "Alpha"
            library.mkdir()
            with ZipFile(library / "Ch 1.cbz", "w") as archive:
                archive.writestr("001.jpg", b"page")
                archive.writestr("ComicInfo.xml", "<ComicInfo><PageCount>2</PageCount></ComicInfo>")
            await library_api.scan(series_id, setup)
            [warning] = await issues_api.list_issues(ignored=False, series_id=series_id, session=setup)
            await issues_api.ignore_issue(warning.id, setup)

        async with maker() as moving, maker() as renaming:
            async def pause_bulk_settings(session):
                if session is moving and not entered.is_set():
                    entered.set()
                    await renamed.wait()
                return await original_get_settings(session)

            monkeypatch.setattr(series_api.settings_service, "get_all", pause_bulk_settings)
            move_task = asyncio.create_task(_edit(
                moving, series_ids=[series_id], root_folder_id=new_root_id, move_files=True,
                monitor_mode="none",
            ))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                [outcome] = await library_api.rename_apply(series_id, RenameApplyIn(), renaming)
                assert outcome.status == "renamed"
            finally:
                renamed.set()
                move = await asyncio.wait_for(move_task, timeout=5)

        assert move.moved == 1 and not move.problems
        moved_path = new / "Alpha" / outcome.new_name
        assert moved_path.is_file() and not library.exists()
        async with maker() as check:
            chapter = await check.scalar(select(Chapter).where(Chapter.series_id == series_id))
            assert chapter.file_path == str(moved_path)
            assert not chapter.monitored
            [ignored] = await issues_api.list_issues(ignored=True, series_id=series_id, session=check)
            assert ignored.id == warning.id and ignored.file_path == str(moved_path)
            await library_api.scan(series_id, check)
            assert await issues_api.list_issues(ignored=False, series_id=series_id, session=check) == []
    finally:
        await engine.dispose()


async def test_move_never_overwrites_and_reports_blocked_series(session, roots):
    old, new = roots
    old_root, new_root = RootFolder(path=str(old)), RootFolder(path=str(new))
    session.add(new_root)
    taken = await _add(session, "Taken", old_root)
    busy = await _add(session, "Busy", old_root)
    fine = await _add(session, "Fine", old_root)
    for name in ("Taken", "Busy", "Fine"):
        (old / name).mkdir()
    (new / "Taken").mkdir()
    session.add(Download(series_id=busy.id, kind=DownloadKind.DIRECT,
                         status=DownloadStatus.DOWNLOADING))
    await session.commit()

    out = await _edit(session, series_ids=[taken.id, busy.id, fine.id],
                      root_folder_id=new_root.id, move_files=True, monitored=False)

    assert [p.title for p in out.problems] == ["Taken", "Busy"]
    assert out.updated == 1 and out.moved == 1
    assert taken.root_folder_id == old_root.id and busy.root_folder_id == old_root.id
    assert (old / "Taken").is_dir() and (old / "Busy").is_dir()
    assert (new / "Fine").is_dir()
    # the rest of the edit still applied to the series that couldn't move
    assert not taken.monitored and not busy.monitored


async def test_bulk_refresh_queues_series_in_the_given_order(session, roots, monkeypatch):
    started = []
    monkeypatch.setattr(series_api, "start_refreshes",
                        lambda ids, **kw: started.append((ids, kw)))
    root = RootFolder(path=str(roots[0]))
    a = await _add(session, "Alpha", root)
    b = await _add(session, "Beta", root)

    out = await series_api.refresh_series_bulk(
        SeriesBulkRefreshIn(series_ids=[b.id, 999, a.id], search_missing=True), session
    )

    assert out.count == 2
    assert started == [([b.id, a.id], {"grab_missing": True, "only_monitored": True})]


async def test_bulk_delete_keeps_files(session, roots):
    old = roots[0]
    root = RootFolder(path=str(old))
    a = await _add(session, "Alpha", root)
    b = await _add(session, "Beta", root)
    (old / "Alpha").mkdir()

    out = await series_api.delete_series_bulk(SeriesBulkIn(series_ids=[a.id]), session)

    assert out.count == 1
    assert await session.scalar(select(func.count(Series.id))) == 1
    assert await session.get(Series, b.id) is not None
    assert (old / "Alpha").is_dir()
