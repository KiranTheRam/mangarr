"""Direct-download worker robustness: a stalled download must fail visibly
instead of wedging the single queue worker, concurrent page callbacks must
not corrupt the shared session, and failures must stay visible in /queue."""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import Update, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.api.library import resync_chapters
from mangarr.api.queue import _remove_downloads, get_queue, retry_failed_download
from mangarr.jobs import tasks
from mangarr.jobs.tasks import REMOVED_BY_USER
from mangarr.models import (
    Base,
    Chapter,
    Download,
    DownloadKind,
    DownloadStatus,
    RootFolder,
    Series,
)
from mangarr.sources.base import DirectSource, SourceChapter


class FakeSource(DirectSource):
    name = "fake"

    async def search_series(self, query):
        return []

    async def list_chapters(self, external_id):
        return [SourceChapter(source_name=self.name, external_id="c1", number=1.0)]

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


async def _make_download(db_session, tmp_path):
    root = RootFolder(path=str(tmp_path))
    series = Series(
        title="Test Series",
        sort_title="test series",
        root_folder=root,
        folder_name="Test Series",
    )
    chapter = Chapter(number=1.0, monitored=True)
    series.chapters.append(chapter)
    db_session.add(series)
    await db_session.commit()
    dl = Download(
        series_id=series.id,
        chapter_id=chapter.id,
        kind=DownloadKind.DIRECT,
        status=DownloadStatus.QUEUED,
        source_name="fake",
        payload="c1",
        title="Test Series - Chapter 1",
    )
    db_session.add(dl)
    await db_session.commit()
    return dl


@pytest.mark.parametrize("replace_existing", [False, True])
async def test_removal_after_direct_publication_rolls_back_the_archive(tmp_path, monkeypatch, replace_existing):
    from mangarr import settings_service

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    monkeypatch.setitem(settings_service.DEFAULTS, "source_fake_enabled", "true")
    defaults = settings_service.DEFAULTS
    dest = tasks.chapter_path(
        tmp_path, defaults["naming_template"], defaults["naming_template_no_volume"],
        "Test Series", "Test Series", 1, None, "",
    )
    published = tasks._publish_direct_archive

    async def stage(source, payload, series, chapter, path, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"new download")

    @asynccontextmanager
    async def remove_after_publish(*args):
        async with published(*args):
            assert dest.read_bytes() == b"new download"
            async with sessions() as remover:
                assert await _remove_downloads(remover, [download_id]) == 1
            yield

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", stage)
    monkeypatch.setattr(tasks, "_publish_direct_archive", remove_after_publish)
    try:
        async with sessions() as worker:
            download = await _make_download(worker, tmp_path)
            download_id, chapter_id = download.id, download.chapter_id
            if replace_existing:
                dest.parent.mkdir()
                dest.write_bytes(b"existing chapter")
                chapter = await worker.get(Chapter, chapter_id)
                chapter.downloaded = True
                chapter.file_path = str(dest)
                await worker.commit()
            await tasks._run_direct_download(worker, download)
        async with sessions() as check:
            download = await check.get(Download, download_id)
            chapter = await check.get(Chapter, chapter_id)
            assert (download.status, download.error) == (DownloadStatus.FAILED, REMOVED_BY_USER)
            assert chapter.downloaded == replace_existing
            assert chapter.file_path == (str(dest) if replace_existing else "")
        if replace_existing:
            assert dest.read_bytes() == b"existing chapter"
        else:
            assert not dest.exists()
    finally:
        await engine.dispose()


async def test_resync_keeps_an_install_journal_until_recovery_succeeds(db_session, tmp_path, monkeypatch):
    async def refresh_without_network(session, series, values):
        series.chapters.append(Chapter(number=1))
        await session.commit()

    monkeypatch.setattr(tasks, "update_chapters", refresh_without_network)
    download = await _make_download(db_session, tmp_path)
    series_id, download_id = download.series_id, download.id
    chapter = await db_session.get(Chapter, download.chapter_id)
    folder = tmp_path / "Test Series"
    folder.mkdir()
    dest = folder / "Test Series - Ch. 0001.cbz"
    dest.write_bytes(b"new download")
    backup = folder / ".rollback" / "previous"
    backup.parent.mkdir()
    backup.write_bytes(b"existing chapter")
    backup_version = list(tasks.archive_version(backup.stat()))
    original_directory = list(tasks.archive_version(folder.stat())[:2])
    download.status = DownloadStatus.FAILED
    download.direct_install = {
        "destination": str(dest), "directory": original_directory,
        "version": list(tasks.archive_version(dest.stat())), "backup": str(backup),
        "previous_versions": [backup_version],
    }
    chapter.downloaded = True
    chapter.file_path = str(dest)
    await db_session.commit()

    offline = tmp_path / "offline"
    folder.rename(offline)
    folder.mkdir()
    with pytest.raises(HTTPException, match="interrupted archive install") as exc:
        await resync_chapters(series_id, db_session)
    assert exc.value.status_code == 409
    assert (await db_session.get(Download, download_id)).direct_install == download.direct_install
    assert (offline / ".rollback" / "previous").read_bytes() == b"existing chapter"

    folder.rmdir()
    offline.rename(folder)
    await resync_chapters(series_id, db_session)
    assert await db_session.get(Download, download_id) is None
    assert dest.read_bytes() == b"existing chapter"
    assert not backup.exists()


async def test_queue_removal_keeps_a_download_that_finished_after_selection(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as remover:
            download = await _make_download(remover, tmp_path)
            download_id = download.id
            execute = remover.execute

            async def finish_after_selection(statement, *args, **kwargs):
                if isinstance(statement, Update):
                    async with sessions.begin() as worker:
                        await worker.execute(update(Download).where(Download.id == download_id).values(
                            status=DownloadStatus.DONE, progress=1.0,
                        ))
                return await execute(statement, *args, **kwargs)

            monkeypatch.setattr(remover, "execute", finish_after_selection)
            assert await _remove_downloads(remover, [download_id]) == 0
        async with sessions() as check:
            download = await check.get(Download, download_id)
            assert download.status == DownloadStatus.DONE
    finally:
        await engine.dispose()


async def test_stalled_download_fails_visibly_instead_of_hanging(
    db_session, tmp_path, monkeypatch
):
    dl = await _make_download(db_session, tmp_path)
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    monkeypatch.setattr(tasks, "DIRECT_STALL_TIMEOUT", timedelta(seconds=0.2))

    cancelled = asyncio.Event()

    async def hanging_download(source, payload, series, chapter, dest,
                               progress_cb=None, cancel_cb=None, web_url="", optimization=None):
        try:
            await asyncio.Event().wait()  # hangs forever
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", hanging_download)

    # the whole point: this returns instead of blocking the queue forever
    await asyncio.wait_for(tasks._run_direct_download(db_session, dl), timeout=10)
    await db_session.refresh(dl)

    assert cancelled.is_set()
    assert dl.status == DownloadStatus.FAILED
    assert "stalled" in dl.error


async def test_concurrent_page_callbacks_do_not_break_the_session(
    db_session, tmp_path, monkeypatch
):
    """Page fetches run concurrently and call cancel/progress callbacks that
    hit the worker's session; unserialized, that raises 'session in prepared
    state' and fails the download."""
    dl = await _make_download(db_session, tmp_path)
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())

    total = 30

    async def concurrent_download(source, payload, series, chapter, dest,
                                  progress_cb=None, cancel_cb=None, web_url="", optimization=None):
        async def page(i):
            await cancel_cb()
            await progress_cb(i + 1, total)
            await cancel_cb()

        async with asyncio.TaskGroup() as tg:
            for i in range(total):
                tg.create_task(page(i))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"downloaded archive")

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", concurrent_download)

    await asyncio.wait_for(tasks._run_direct_download(db_session, dl), timeout=10)
    await db_session.refresh(dl)

    assert dl.error == ""
    assert dl.status == DownloadStatus.DONE


async def test_failed_downloads_stay_visible_in_queue(db_session, tmp_path):
    dl = await _make_download(db_session, tmp_path)
    dl.status = DownloadStatus.FAILED
    dl.error = "stalled: no page finished for 15 minutes"
    await db_session.commit()

    items = await get_queue(db_session)
    assert [i.id for i in items] == [dl.id]
    assert items[0].error == dl.error

    # dismissing the failure hides it and clears the retry blacklist marker
    assert await _remove_downloads(db_session, [dl.id]) == 1
    await db_session.refresh(dl)
    assert dl.error == REMOVED_BY_USER
    assert await get_queue(db_session) == []


async def test_disabled_source_is_rejected_before_network(db_session, tmp_path, monkeypatch):
    from mangarr import settings_service
    from unittest.mock import AsyncMock
    dl = await _make_download(db_session, tmp_path)
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    await settings_service.set_many(db_session, {"source_fake_enabled": "false"})
    download = AsyncMock()
    monkeypatch.setattr(tasks, "download_chapter_to_cbz", download)
    await tasks._run_direct_download(db_session, dl)
    download.assert_not_awaited()
    assert dl.status == DownloadStatus.FAILED
    assert "disabled" in dl.error


async def test_retry_reuses_the_staging_path_a_crash_could_leave(db_session, tmp_path, monkeypatch):
    dl = await _make_download(db_session, tmp_path)
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    staged_paths = []

    async def failing_download(source, payload, series, chapter, dest,
                               progress_cb=None, cancel_cb=None, web_url="", optimization=None):
        staged_paths.append(dest)
        raise RuntimeError("source went away")

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", failing_download)
    for _ in range(2):
        await tasks._run_direct_download(db_session, dl)
        await db_session.refresh(dl)
        assert dl.status == DownloadStatus.FAILED
        dl.status = DownloadStatus.QUEUED
        await db_session.commit()

    first, second = staged_paths
    assert first == second
    assert first.name == f".mangarr-direct-{dl.id}.staged"
    assert not list(first.parent.glob(".mangarr-direct-*"))


async def test_download_preserves_an_unmapped_archive_that_appears_mid_download(db_session, tmp_path, monkeypatch):
    from mangarr import settings_service

    download = await _make_download(db_session, tmp_path)
    chapter_id = download.chapter_id
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    defaults = settings_service.DEFAULTS
    destination = tasks.chapter_path(
        tmp_path, defaults["naming_template"], defaults["naming_template_no_volume"],
        "Test Series", "Test Series", 1.0, None, "",
    )

    async def compete(source, payload, series, chapter, staged, **kwargs):
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"direct contents")
        destination.write_bytes(b"torrent contents")

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", compete)
    await tasks._run_direct_download(db_session, download)

    assert destination.read_bytes() == b"torrent contents"
    assert download.status == DownloadStatus.FAILED
    assert "already exists" in download.error
    chapter = await db_session.get(Chapter, chapter_id)
    assert not chapter.downloaded and not chapter.file_path


@pytest.mark.parametrize("previous", [None, "archive", "symlink", "concurrent replacement"])
async def test_failed_install_leaves_the_direct_download_retryable(db_session, tmp_path, monkeypatch, previous):
    from mangarr import settings_service

    download = await _make_download(db_session, tmp_path)
    download_id, chapter_id = download.id, download.chapter_id
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    defaults = settings_service.DEFAULTS
    dest = tasks.chapter_path(
        tmp_path, defaults["naming_template"], defaults["naming_template_no_volume"],
        "Test Series", "Test Series", 1, None, "",
    )
    if previous is not None:
        dest.parent.mkdir()
        if previous == "symlink":
            target = tmp_path / "original.cbz"
            target.write_bytes(b"original")
            dest.symlink_to("../original.cbz")
        else:
            dest.write_bytes(b"original")
        chapter = await db_session.get(Chapter, chapter_id)
        chapter.file_path = str(dest)
        chapter.downloaded = True
        await db_session.commit()

    async def stage(source, payload, series, chapter, staged, **kwargs):
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"new download")

    commit = db_session.commit

    async def fail_install_commit():
        if previous == "concurrent replacement" and download.direct_install is not None:
            await commit()
            other = dest.with_suffix(".incoming")
            other.write_bytes(b"replacement between validation and publication")
            other.replace(dest)
            monkeypatch.setattr(db_session, "commit", commit)
            return
        if download.status == DownloadStatus.DONE:
            assert dest.read_bytes() == b"new download"
            monkeypatch.setattr(db_session, "commit", commit)
            raise RuntimeError("install commit failed")
        await commit()

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", stage)
    monkeypatch.setattr(db_session, "commit", fail_install_commit)
    await tasks._run_direct_download(db_session, download)

    assert download.status == DownloadStatus.FAILED
    if previous == "concurrent replacement":
        assert "changed while the download was being installed" in download.error
    else:
        assert download.error == "install commit failed"
    chapter = await db_session.get(Chapter, chapter_id)
    if previous is None:
        assert not dest.exists()
        assert not chapter.downloaded and not chapter.file_path
    else:
        if previous == "concurrent replacement":
            assert dest.read_bytes() == b"replacement between validation and publication"
        else:
            assert dest.read_bytes() == b"original"
        assert dest.is_symlink() == (previous == "symlink")
        assert chapter.downloaded and chapter.file_path == str(dest)

    await retry_failed_download(download_id, db_session)
    await tasks._run_direct_download(db_session, download)

    assert download.status == DownloadStatus.DONE
    assert chapter.downloaded and chapter.file_path == str(dest)
    assert dest.read_bytes() == b"new download"
    assert not list(dest.parent.glob(".mangarr-direct-*"))


async def _crash_during_publication(database, destination, commit_mapping):
    engine = create_async_engine(database)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        download = await session.get(Download, 1)
        series = await tasks._load_series(session, download.series_id)
        chapter = series.chapters[0]
        dest = Path(destination)
        staged = dest.with_name(".mangarr-direct-1.staged")
        staged.write_bytes(b"new download")
        expected = tasks.archive_version(dest.stat()) if dest.exists() else None
        commit = session.commit

        async def interrupt_install_commit():
            if download.status == DownloadStatus.DONE:
                assert dest.read_bytes() == b"new download"
                if commit_mapping:
                    await commit()
                os._exit(75)
            await commit()

        async def wanted():
            pass

        session.commit = interrupt_install_commit
        await tasks._install_direct_download(session, download, series, chapter, staged, dest, wanted, expected)


@pytest.mark.parametrize("scenario", [
    "new file", "overwrite", "committed overwrite", "external replacement", "hidden directory",
])
async def test_startup_recovers_a_direct_worker_killed_during_install(tmp_path, monkeypatch, scenario):
    from mangarr import settings_service

    root = tmp_path / "library"
    root.mkdir()
    database = f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}"
    engine = create_async_engine(database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session

    monkeypatch.setattr(tasks, "session_scope", scope)
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    monkeypatch.setitem(settings_service.DEFAULTS, "source_fake_enabled", "true")
    defaults = settings_service.DEFAULTS
    dest = tasks.chapter_path(
        root, defaults["naming_template"], defaults["naming_template_no_volume"],
        "Test Series", "Test Series", 1, None, "",
    )
    dest.parent.mkdir()
    overwrite = scenario in {"overwrite", "committed overwrite"}
    committed = scenario == "committed overwrite"

    try:
        async with maker() as session:
            download = await _make_download(session, root)
            download.status = DownloadStatus.DOWNLOADING
            chapter = await session.get(Chapter, download.chapter_id)
            if overwrite:
                dest.write_bytes(b"original")
                chapter.downloaded = True
                chapter.file_path = str(dest)
            await session.commit()

        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import asyncio, sys; from tests.test_direct_stall import _crash_during_publication; "
            "asyncio.run(_crash_during_publication(sys.argv[1], sys.argv[2], sys.argv[3] == 'True'))",
            database, str(dest), str(committed), cwd=Path(__file__).resolve().parents[1],
        )
        assert await asyncio.wait_for(process.wait(), timeout=10) == 75
        assert dest.read_bytes() == b"new download"
        if scenario == "external replacement":
            other = dest.with_suffix(".incoming")
            other.write_bytes(b"different archive")
            other.replace(dest)
        if scenario == "hidden directory":
            hidden = dest.parent.with_name("offline")
            dest.parent.rename(hidden)
            dest.parent.mkdir()
            await tasks.recover_interrupted_downloads()
            async with maker() as session:
                assert (await session.get(Download, 1)).direct_install is not None
            dest.parent.rmdir()
            hidden.rename(dest.parent)

        await tasks.recover_interrupted_downloads()
        async with maker() as session:
            download = await session.get(Download, 1)
            assert download.direct_install is None
            assert not list(dest.parent.glob(".mangarr-direct-rollback-*"))
            if committed:
                assert download.status == DownloadStatus.DONE
                assert dest.read_bytes() == b"new download"
            else:
                assert download.status == DownloadStatus.QUEUED
                if overwrite:
                    assert dest.read_bytes() == b"original"
                elif scenario == "external replacement":
                    assert dest.read_bytes() == b"different archive"
                else:
                    assert not dest.exists()

                async def stage(source, payload, series, chapter, staged, **kwargs):
                    staged.write_bytes(b"retried download")

                monkeypatch.setattr(tasks, "download_chapter_to_cbz", stage)
                await tasks._run_direct_download(session, download)
                if scenario == "external replacement":
                    assert download.status == DownloadStatus.FAILED
                    assert dest.read_bytes() == b"different archive"
                else:
                    assert download.status == DownloadStatus.DONE
                    assert dest.read_bytes() == b"retried download"
    finally:
        await engine.dispose()


async def test_download_does_not_install_in_the_old_root_after_a_root_change(db_session, tmp_path, monkeypatch):
    original_root = tmp_path / "original"
    original_root.mkdir()
    new_root = tmp_path / "new"
    new_root.mkdir()
    download = await _make_download(db_session, original_root)
    chapter_id = download.chapter_id
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())

    async def change_root(source, payload, series, chapter, staged, **kwargs):
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"download contents")
        series.root_folder = RootFolder(path=str(new_root))
        await db_session.commit()

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", change_root)
    await tasks._run_direct_download(db_session, download)

    assert download.status == DownloadStatus.FAILED
    assert "destination changed" in download.error
    assert not list(original_root.rglob("*.cbz"))
    assert not list(original_root.rglob("*.staged"))
    chapter = await db_session.get(Chapter, chapter_id)
    assert not chapter.downloaded and not chapter.file_path


async def test_download_refuses_to_replace_a_file_another_series_maps(db_session, tmp_path, monkeypatch):
    from mangarr import settings_service

    dl = await _make_download(db_session, tmp_path)
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    series = await db_session.get(Series, dl.series_id)
    chapter = await db_session.get(Chapter, dl.chapter_id)
    defaults = settings_service.DEFAULTS
    dest = tasks.chapter_path(
        tmp_path, defaults["naming_template"], defaults["naming_template_no_volume"],
        series.title, series.folder_name, chapter.number, chapter.volume, chapter.title,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"another series' archive")
    db_session.add(Series(
        title="Other", sort_title="other", folder_name="Other", root_folder_id=series.root_folder_id,
        chapters=[Chapter(number=5.0, downloaded=True, file_path=str(dest))],
    ))
    await db_session.commit()

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("the download must not start")

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", unexpected)
    await tasks._run_direct_download(db_session, dl)
    await db_session.refresh(dl)

    assert dl.status == DownloadStatus.FAILED
    assert "mapped by another chapter" in dl.error
    assert dest.read_bytes() == b"another series' archive"
