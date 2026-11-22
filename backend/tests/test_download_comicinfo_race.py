import asyncio
import threading
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr import comicinfo, metadata_issues, settings_service
from mangarr.api import issues
from mangarr.download.cbz import write_cbz
from mangarr.jobs import tasks
from mangarr.library.matcher import find_media_files
from mangarr.models import Base, Chapter, Download, DownloadKind, DownloadStatus, RootFolder, Series
from mangarr.schemas import ComicInfoUpdateIn


async def test_direct_upgrade_waits_for_repair_before_installing_archive(tmp_path, monkeypatch):
    folder = tmp_path / "Tracked"
    old = folder / "Tracked Ch. 1.cbz"
    pages = [b"\xff\xd8\xffpage"]
    write_cbz(old, pages, "<ComicInfo><Number>1</Number><Title>Old</Title></ComicInfo>")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tasks, "_SERIES_LOCKS", {})
    archive_lock = asyncio.Lock()
    monkeypatch.setattr(issues, "manual_archive_lock", archive_lock)
    monkeypatch.setattr(metadata_issues, "manual_archive_lock", archive_lock)
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fixture", object())

    async def apply_settings(session):
        values = await settings_service.get_all(session)
        return {**values, "source_fixture_enabled": "true"}

    monkeypatch.setattr(tasks.registry, "apply_settings", apply_settings)
    staged = asyncio.Event()
    ready_to_replace = asyncio.Event()
    release_repair = threading.Event()
    used_revision = comicinfo._require_revision
    loop = asyncio.get_running_loop()

    async def fetch(_source, _payload, _series, _chapter, destination, **kwargs):
        await kwargs["cancel_cb"]()
        write_cbz(destination, pages, "<ComicInfo><Number>1</Number><Title>Upgrade</Title></ComicInfo>")
        staged.set()

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", fetch)

    def pause_repair(path, revision):
        used_revision(path, revision)
        if path == old and any(folder.glob(".mangarr-comicinfo-*.tmp")):
            loop.call_soon_threadsafe(ready_to_replace.set)
            assert release_repair.wait(timeout=5)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            chapter = Chapter(number=1, downloaded=True, file_path=str(old), file_source="fixture")
            series = Series(
                title="Tracked", root_folder=RootFolder(path=str(tmp_path)), chapters=[chapter],
            )
            session.add(series)
            await session.flush()
            download = Download(
                series_id=series.id, chapter_id=chapter.id, kind=DownloadKind.DIRECT,
                source_name="fixture", payload="chapter1", status=DownloadStatus.QUEUED,
            )
            session.add(download)
            await session.commit()
            series_id, chapter_id, download_id = series.id, chapter.id, download.id
            before = await issues.get_chapter_comicinfo(series_id, chapter_id, session)
        monkeypatch.setattr(comicinfo, "_require_revision", pause_repair)

        async def repair():
            async with sessions() as session:
                return await issues.update_chapter_comicinfo(
                    series_id, chapter_id, ComicInfoUpdateIn(revision=before.revision, title="Edited"), session,
                )

        async def upgrade():
            async with sessions() as session:
                await tasks._run_direct_download(session, await session.get(Download, download_id))

        async with asyncio.timeout(10):
            repair_task = asyncio.create_task(repair())
            upgrade_task = None
            try:
                await ready_to_replace.wait()
                upgrade_task = asyncio.create_task(upgrade())
                await staged.wait()
                assert [item.path for item in find_media_files(folder)] == [old]
                async with sessions() as session:
                    assert (await session.get(Chapter, chapter_id)).file_path == str(old)
                    assert (await session.get(Download, download_id)).status == DownloadStatus.DOWNLOADING
            finally:
                release_repair.set()
                repair_result = await repair_task
                if upgrade_task is not None:
                    await upgrade_task

        assert repair_result.title == "Edited"
        async with sessions() as session:
            archive = Path((await session.get(Chapter, chapter_id)).file_path)
            assert (await session.get(Download, download_id)).status == DownloadStatus.DONE
        assert archive != old and not old.exists()
        assert list(folder.iterdir()) == [archive]
        assert comicinfo.read_comicinfo(archive).title == "Upgrade"
    finally:
        await engine.dispose()
