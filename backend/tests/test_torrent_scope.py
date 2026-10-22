"""#23: a finished torrent imports only its own files, never its neighbours'
in the shared qBittorrent category folder ("Don't create subfolder")."""

import zipfile
from contextlib import asynccontextmanager

import pytest
import respx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.download.qbittorrent import QbtClient
from mangarr.jobs import tasks
from mangarr.library.naming import DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME
from mangarr.models import (
    Base, Chapter, Download, DownloadKind, DownloadStatus, RootFolder, Series,
)

BASE = "http://qbt:8080"
HASH = "c" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
VALUES = {
    "qbittorrent_enabled": "true",
    "qbittorrent_url": BASE,
    "qbittorrent_username": "admin",
    "qbittorrent_password": "pw",
    "naming_template": DEFAULT_TEMPLATE,
    "naming_template_no_volume": DEFAULT_TEMPLATE_NO_VOLUME,
    "import_mode": "hardlink",
}


def make_cbz(path, name="001.png"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(name, PNG)


@pytest.fixture
def category(tmp_path):
    """The shared category folder, already holding an older torrent's payload
    (the reported repro: Dandadan c001-002 landing in Kagurabachi)."""
    folder = tmp_path / "downloads" / "mangarr"
    make_cbz(folder / "Dandadan c001-002" / "Dandadan - c001.cbz")
    make_cbz(folder / "Dandadan c001-002" / "Dandadan - c002.cbz")
    return folder


@pytest.fixture
async def env(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    (tmp_path / "library").mkdir()  # the API creates a root on add
    async with maker() as session:
        series = Series(title="Kagurabachi", sort_title="kagurabachi",
                        root_folder=RootFolder(path=str(tmp_path / "library")),
                        folder_name="Kagurabachi")
        series.chapters = [Chapter(number=float(n), downloaded=False) for n in (1, 2, 3)]
        session.add(series)
        await session.commit()
        dl = Download(series_id=series.id, kind=DownloadKind.TORRENT,
                      status=DownloadStatus.DOWNLOADING, title="Kagurabachi c003",
                      torrent_hash=HASH)
        session.add(dl)
        await session.commit()

        @asynccontextmanager
        async def scope():
            yield session

        async def apply_settings(session):
            return dict(VALUES)

        monkeypatch.setattr(tasks, "session_scope", scope)
        monkeypatch.setattr(tasks.registry, "apply_settings", apply_settings)
        monkeypatch.setattr(tasks.notifications, "notify_import", lambda *a: None)
        monkeypatch.setattr(tasks, "_notify_kavita", lambda *a: None)
        yield session, series, dl, tmp_path / "library" / "Kagurabachi"
    tasks._import_path_missing_counts.clear()
    await engine.dispose()


async def sync(session, series, dl, *, content_path, save_path, files):
    """One sync pass against a qBittorrent reporting a finished torrent."""
    with respx.mock(base_url=f"{BASE}/api/v2", assert_all_called=False) as qbt:
        qbt.post("/auth/login").respond(200, text="Ok.")
        qbt.get("/torrents/info").respond(json=[{
            "hash": HASH, "name": "Kagurabachi c003", "progress": 1.0,
            "state": "stalledUP", "category": "mangarr",
            "content_path": str(content_path), "save_path": str(save_path),
        }])
        qbt.get("/torrents/files").respond(json=[
            {"name": name, "priority": priority} for name, priority in files
        ])
        await tasks.sync_qbittorrent()
    await session.refresh(dl)
    await session.refresh(series, ["chapters"])
    return {c.number for c in series.chapters if c.downloaded}


async def test_no_subfolder_imports_only_own_files(env, category):
    session, series, dl, library = env
    make_cbz(category / "Kagurabachi - c003.cbz")
    # without a root folder qBittorrent reports content_path == save_path
    downloaded = await sync(session, series, dl, content_path=category, save_path=category,
                            files=[("Kagurabachi - c003.cbz", 1),
                                   ("Kagurabachi - c004.cbz", 0)])  # unwanted: not on disk

    assert dl.status == DownloadStatus.DONE, dl.error
    assert downloaded == {3.0}  # the Dandadan files are not chapters 1-2
    assert sorted(p.name for p in library.iterdir()) == ["Kagurabachi - Ch. 0003.cbz"]


async def test_neighbours_numbering_does_not_fail_the_import(env, tmp_path):
    session, series, dl, library = env
    # another torrent's chapter 1 sits beside ours in the shared folder; the
    # duplicate pre-check must only see this torrent's files, or it reads the
    # two as one torrent mapping several files to chapter 1
    shared = tmp_path / "downloads" / "mangarr"
    make_cbz(shared / "Kagurabachi Ch. 001.cbz")
    make_cbz(shared / "Berserk Ch. 001.cbz")
    downloaded = await sync(session, series, dl, content_path=shared, save_path=shared,
                            files=[("Kagurabachi Ch. 001.cbz", 1)])

    assert dl.status == DownloadStatus.DONE, dl.error
    assert downloaded == {1.0}
    assert sorted(p.name for p in library.iterdir()) == ["Kagurabachi - Ch. 0001.cbz"]


async def test_no_subfolder_loose_pages_are_not_packed_with_neighbours(env, category):
    session, series, dl, library = env
    pages = category / "Kagurabachi c003"
    pages.mkdir()
    (pages / "01.png").write_bytes(PNG)
    (category / "stray.png").write_bytes(PNG)  # another torrent's single page
    # our torrent's own pages sit at the category root too
    (category / "02.png").write_bytes(PNG)

    await sync(session, series, dl, content_path=category, save_path=category,
               files=[("Kagurabachi c003/01.png", 1), ("02.png", 1)])

    assert dl.status == DownloadStatus.DONE, dl.error
    packed = {}
    for cbz in library.iterdir():
        with zipfile.ZipFile(cbz) as zf:
            packed[cbz.name] = zf.namelist()
    assert packed["Kagurabachi - Ch. 0003.cbz"] == ["01.png"]
    assert ["02.png"] in packed.values()
    assert not any("stray.png" in names for names in packed.values())


async def test_subfolder_torrent_unchanged(env, category):
    session, series, dl, library = env
    content = category / "Kagurabachi c003"
    make_cbz(content / "Kagurabachi - c003.cbz")

    downloaded = await sync(session, series, dl, content_path=content, save_path=category,
                            files=[("Kagurabachi c003/Kagurabachi - c003.cbz", 1)])

    assert dl.status == DownloadStatus.DONE, dl.error
    assert downloaded == {3.0}
    assert sorted(p.name for p in library.iterdir()) == ["Kagurabachi - Ch. 0003.cbz"]


async def test_single_file_torrent(env, category):
    session, series, dl, library = env
    content = category / "Kagurabachi - c003.cbz"
    make_cbz(content)

    downloaded = await sync(session, series, dl, content_path=content, save_path=category,
                            files=[("Kagurabachi - c003.cbz", 1)])

    assert dl.status == DownloadStatus.DONE, dl.error
    assert downloaded == {3.0}
    assert sorted(p.name for p in library.iterdir()) == ["Kagurabachi - Ch. 0003.cbz"]


async def test_listed_file_gone_retries_instead_of_finishing_empty(env, category):
    session, series, dl, library = env
    # the shared folder exists, but this torrent's file is not there (moved
    # mid-import) — previously the content_path check passed and nothing imported
    downloaded = await sync(session, series, dl, content_path=category, save_path=category,
                            files=[("Kagurabachi - c003.cbz", 1)])

    assert dl.status == DownloadStatus.DOWNLOADING
    assert "content moved during import; retrying" in dl.error
    assert downloaded == set()


async def test_deleted_sidecar_file_does_not_block_the_import(env, category):
    session, series, dl, library = env
    make_cbz(category / "Kagurabachi - c003.cbz")
    # qBittorrent still lists the .nfo the user deleted; only media must exist
    downloaded = await sync(session, series, dl, content_path=category, save_path=category,
                            files=[("Kagurabachi - c003.cbz", 1), ("release.nfo", 1)])

    assert dl.status == DownloadStatus.DONE
    assert downloaded == {3.0}


async def test_no_listed_files_fails_visibly(env, category):
    session, series, dl, library = env
    downloaded = await sync(session, series, dl, content_path=category, save_path=category,
                            files=[])

    assert dl.status == DownloadStatus.FAILED
    assert "no files" in dl.error
    assert downloaded == set()
