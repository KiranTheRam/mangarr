"""An extra series folder can be a mount separate from the library root."""

from pathlib import Path
from zipfile import ZipFile

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr import db
from mangarr.api import issues as issues_api, library as library_api
from mangarr.jobs import tasks
from mangarr.models import Base, Chapter, LibraryIssue, RootFolder, Series, SeriesFolder


def _archive(path: Path, chapter: int, volume: int = 1) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr("001.png", b"page")
        archive.writestr(
            "ComicInfo.xml",
            f"<ComicInfo><Number>{chapter}</Number><Volume>{volume}</Volume></ComicInfo>",
        )


@pytest.mark.parametrize("mount_state", ["missing", "empty"])
@pytest.mark.parametrize("relative", [False, True])
async def test_scans_preserve_ignored_warning_during_extra_folder_outage(
    tmp_path, monkeypatch, mount_state, relative,
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    monkeypatch.setattr(tasks, "_SERIES_LOCKS", {})
    root = tmp_path / "library"
    main = root / "Series"
    main.mkdir(parents=True)
    extra = root / "chapters" if relative else tmp_path / "chapters"
    extra.mkdir()
    extra_path = extra / "Series Ch. 1.cbz"
    good_path = main / "Series Ch. 2.cbz"
    gone_path = main / "Series Ch. 3.cbz"
    _archive(extra_path, 1, volume=9)
    _archive(good_path, 2)
    async with sessions() as session:
        series = Series(title="Series", folder_name=main.name, root_folder=RootFolder(path=str(root)))
        series.extra_folders = [SeriesFolder(path=extra.name if relative else str(extra))]
        series.chapters = [
            Chapter(number=1, volume=1, volume_source="viz", downloaded=True, file_path=str(extra_path)),
            Chapter(number=2, volume=1, volume_source="viz"),
            Chapter(number=3, volume=1, volume_source="viz", downloaded=True, file_path=str(gone_path)),
        ]
        session.add(series)
        await session.commit()
        series_id = series.id
        await issues_api.scan_issues(session)
        issue_id = (await session.scalar(select(LibraryIssue))).public_id
        await issues_api.ignore_issue(issue_id, session)

    offline = tmp_path / "offline"
    extra.rename(offline)
    if mount_state == "empty":
        extra.mkdir()
    async with sessions() as session:
        # A metadata-only scan must also preserve warnings for an empty
        # mount point where the archive's parent directory still exists.
        assert (await issues_api.scan_issues(session)).ignored == 1
        result = await library_api.scan(series_id, session)
        assert result.cleared == 1
        assert result.matched_chapters == 1
    await tasks.scan_all_series()
    async with sessions() as session:
        series = await tasks._load_series(session, series_id)
        assert await tasks.reconcile_downloaded_files(session, series) == 0
        chapters = {chapter.number: chapter for chapter in series.chapters}
        assert chapters[1].downloaded and chapters[1].file_path == str(extra_path)
        assert chapters[2].downloaded and chapters[2].file_path == str(good_path)
        assert not chapters[3].downloaded and not chapters[3].file_path
        issue = await session.scalar(select(LibraryIssue))
        assert issue is not None and issue.ignored and issue.public_id == issue_id

    if mount_state == "empty":
        extra.rmdir()
    offline.rename(extra)
    await tasks.scan_all_series()
    async with sessions() as session:
        issue = await session.scalar(select(LibraryIssue))
        assert issue is not None and issue.ignored and issue.public_id == issue_id

    # A missing archive in a reachable extra should still be reconciled.
    (extra / ".keep").touch()
    extra_path.unlink()
    async with sessions() as session:
        result = await library_api.scan(series_id, session)
        assert result.cleared == 1
        assert await session.scalar(select(LibraryIssue)) is None
    await engine.dispose()
