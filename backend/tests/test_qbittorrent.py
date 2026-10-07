import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.download.qbittorrent import QbtClient, QbtTorrent
from mangarr.jobs import tasks
from mangarr.jobs.tasks import enqueue_torrent, torrent_save_path
from mangarr.models import Base, Download, DownloadKind, DownloadStatus, RootFolder, Series

BASE = "http://qbt:8080"


def client():
    c = QbtClient(BASE, "admin", "pw")
    c._logged_in = True  # skip the login round-trip
    return c


@respx.mock
async def test_default_save_path():
    respx.get(f"{BASE}/api/v2/app/preferences").respond(json={"save_path": "/downloads"})
    assert await client().default_save_path() == "/downloads"


@respx.mock
async def test_default_save_path_missing_is_empty():
    respx.get(f"{BASE}/api/v2/app/preferences").respond(json={})
    assert await client().default_save_path() == ""


@respx.mock
async def test_ensure_category_created():
    route = respx.post(f"{BASE}/api/v2/torrents/createCategory").respond(200)
    await client().ensure_category("mangarr", "/downloads/mangarr")
    assert route.called
    sent = route.calls.last.request.content.decode()
    assert "category=mangarr" in sent and "savePath=%2Fdownloads%2Fmangarr" in sent


@respx.mock
async def test_ensure_category_conflict_edits():
    respx.post(f"{BASE}/api/v2/torrents/createCategory").respond(409)
    edit = respx.post(f"{BASE}/api/v2/torrents/editCategory").respond(200)
    await client().ensure_category("mangarr", "/downloads/mangarr")
    assert edit.called  # already exists → path updated instead


@respx.mock
async def test_add_magnet_sends_category_and_savepath():
    route = respx.post(f"{BASE}/api/v2/torrents/add").respond(200, text="Ok.")
    await client().add_magnet("magnet:?xt=urn:btih:abc", category="mangarr",
                              save_path="/downloads/mangarr")
    body = route.calls.last.request.content.decode()
    assert "category=mangarr" in body
    assert "savepath=%2Fdownloads%2Fmangarr" in body
    assert "autoTMM=false" in body


async def test_enqueue_torrent_rejects_magnet_without_btih():
    with pytest.raises(ValueError, match="btih"):
        await enqueue_torrent(None, None, "magnet:?dn=nohash", "bad", {})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("base", "category", "expected"),
    [
        ("/media/torrents", "mangarr", "/media/torrents/mangarr"),
        ("/media/torrents/", "mangarr", "/media/torrents/mangarr"),
        ("/media/torrents/mangarr", "mangarr", "/media/torrents/mangarr"),
        ("/", "mangarr", "/mangarr"),
        ("", "mangarr", None),
    ],
)
def test_torrent_save_path_accepts_root_or_legacy_final_path(base, category, expected):
    assert torrent_save_path(base, category) == expected


def test_complete_progress_does_not_bypass_qbittorrent_move():
    moving = QbtTorrent("hash", "name", 1.0, "moving", "/tmp/name", "mangarr")
    uploading = QbtTorrent("hash", "name", 1.0, "uploading", "/final/name", "mangarr")

    assert not moving.is_complete
    assert uploading.is_complete


async def test_import_retries_when_qbittorrent_moves_content(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with maker() as session:
            (tmp_path / "library").mkdir()  # the API creates a root on add
            root = RootFolder(path=str(tmp_path / "library"))
            series = Series(
                title="Vinland Saga",
                sort_title="vinland saga",
                root_folder=root,
                folder_name="Vinland Saga",
            )
            session.add(series)
            await session.commit()
            download = Download(
                series_id=series.id,
                kind=DownloadKind.TORRENT,
                status=DownloadStatus.IMPORTING,
                title="Vinland Saga pack",
                torrent_hash="a" * 40,
            )
            session.add(download)
            await session.commit()

            content = tmp_path / "incomplete" / "Vinland Saga"
            content.mkdir(parents=True)

            def moved_during_import(*args, **kwargs):
                raise FileNotFoundError(content / "Vinland Saga v10.cbz")

            monkeypatch.setattr(tasks, "import_torrent_payload", moved_during_import)
            await tasks._import_torrent(
                session,
                download,
                content,
                {
                    "naming_template": "{series} - Ch. {chapter:04.1f}",
                    "naming_template_no_volume": "{series} - Ch. {chapter:04.1f}",
                    "import_mode": "hardlink",
                },
            )
            await session.refresh(download)

            assert download.status == DownloadStatus.DOWNLOADING
            assert "content moved during import; retrying" in download.error
    finally:
        tasks._import_path_missing_counts.clear()
        await engine.dispose()


async def test_torrent_import_maps_english_book_to_actual_chapters(tmp_path, monkeypatch):
    import zipfile
    from mangarr.models import Chapter
    from mangarr.library.naming import DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME

    engine = create_async_engine('sqlite+aiosqlite://')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            (tmp_path / 'library').mkdir()  # the API creates a root on add
            series = Series(title='Vinland Saga', sort_title='vinland saga',
                            root_folder=RootFolder(path=str(tmp_path / 'library')),
                            folder_name='Vinland Saga')
            series.chapters = [Chapter(number=n, volume=v, downloaded=False)
                               for n, v in ((16., 2), (17., 3), (28.5, 4))]
            session.add(series)
            await session.commit()
            download = Download(series_id=series.id, kind=DownloadKind.TORRENT,
                                status=DownloadStatus.IMPORTING, title='English book 2')
            session.add(download)
            await session.commit()
            source = tmp_path / 'Vinland Saga v02.cbz'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('Vinland Saga c017 (v03) - p001.jpg', b'page')
                archive.writestr('Vinland Saga c028x5 (v04) - p441.png', b'page')
            monkeypatch.setattr(tasks.notifications, 'notify_import', lambda *a: None)
            monkeypatch.setattr(tasks, '_notify_kavita', lambda *a: None)
            await tasks._import_torrent(session, download, source, {
                'naming_template': DEFAULT_TEMPLATE,
                'naming_template_no_volume': DEFAULT_TEMPLATE_NO_VOLUME,
                'import_mode': 'copy',
            })
            await session.refresh(download)
            assert download.status == DownloadStatus.DONE
            await session.refresh(series, ['chapters'])
            by_number = {c.number: c for c in series.chapters}
            assert not by_number[16.].downloaded
            for n in (17., 28.5):
                assert by_number[n].downloaded
                assert by_number[n].file_path.endswith('Vinland Saga - Vol. 02.cbz')
    finally:
        await engine.dispose()
