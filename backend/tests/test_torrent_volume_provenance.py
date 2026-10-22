import zipfile

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.jobs import tasks
from mangarr.library.naming import DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME
from mangarr.models import (
    Base, Chapter, Download, DownloadKind, DownloadStatus, RootFolder, Series,
)


async def test_volume_archive_import_records_nyaa_as_file_source(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        (tmp_path / "library").mkdir()  # the API creates a root on add
        async with maker() as session:
            series = Series(title="Vinland Saga", sort_title="vinland saga",
                            root_folder=RootFolder(path=str(tmp_path / "library")),
                            folder_name="Vinland Saga")
            series.chapters = [Chapter(number=n, volume=2, downloaded=False)
                               for n in (9., 10.)]
            session.add(series)
            await session.commit()
            download = Download(series_id=series.id, kind=DownloadKind.TORRENT,
                                status=DownloadStatus.IMPORTING, title="Vinland Saga v02")
            session.add(download)
            await session.commit()
            source = tmp_path / "Vinland Saga v02.cbz"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("Vinland Saga c009 - p001.jpg", b"page")
                archive.writestr("Vinland Saga c010 - p001.jpg", b"page")
            monkeypatch.setattr(tasks.notifications, "notify_import", lambda *a: None)
            monkeypatch.setattr(tasks, "_notify_kavita", lambda *a: None)
            await tasks._import_torrent(session, download, source, {
                "naming_template": DEFAULT_TEMPLATE,
                "naming_template_no_volume": DEFAULT_TEMPLATE_NO_VOLUME,
                "import_mode": "copy",
            })
            await session.refresh(download)
            assert download.status == DownloadStatus.DONE
            await session.refresh(series, ["chapters"])
            for chapter in series.chapters:
                assert chapter.downloaded
                assert chapter.file_path.endswith("Vinland Saga - Vol. 02.cbz")
                assert chapter.file_source == "nyaa"
    finally:
        await engine.dispose()
