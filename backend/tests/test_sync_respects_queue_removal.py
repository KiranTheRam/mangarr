"""A queue removal that commits while sync_qbittorrent is awaiting qBittorrent
stands: the sync must not write the row's stale status back over it."""

import asyncio
import shutil
import zipfile
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr import settings_service
from mangarr.api import queue as queue_api
from mangarr.download.qbittorrent import QbtTorrent
from mangarr.jobs import tasks
from mangarr.models import (
    Base, Chapter, Download, DownloadKind, DownloadStatus, RootFolder, Series,
)

HASH = "e" * 40
NAME = "Test Series c001"


@pytest.fixture
async def maker(tmp_path):
    # a file database, so the sync and the API request really are two
    # connections, as in the app
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)  # as db.SessionLocal
    tasks._torrent_missing_counts.clear()
    tasks._import_path_missing_counts.clear()
    await engine.dispose()


class FakeQbt:
    """qBittorrent holding one finished torrent. A test can hold a call open
    (`hold` = "get_torrent" or "torrent_files") until it sets `release`."""

    payload = None
    present = True
    hold = None
    entered: asyncio.Event
    release: asyncio.Event

    def __init__(self, *args):
        pass

    @classmethod
    def reset(cls, payload, hold=None, present=True):
        cls.payload, cls.hold, cls.present = payload, hold, present
        cls.entered, cls.release = asyncio.Event(), asyncio.Event()

    async def _maybe_hold(self, call):
        if FakeQbt.hold == call:
            FakeQbt.hold = None  # once
            FakeQbt.entered.set()
            await FakeQbt.release.wait()

    async def get_torrent(self, torrent_hash):
        await self._maybe_hold("get_torrent")
        if not FakeQbt.present:
            return None
        return QbtTorrent(hash=torrent_hash, name=NAME, progress=1.0, state="uploading",
                          content_path=str(FakeQbt.payload), category="mangarr",
                          save_path=str(FakeQbt.payload.parent))

    async def torrent_files(self, torrent_hash):
        await self._maybe_hold("torrent_files")
        return [f"{NAME}/Test Series - c001.cbz"]

    async def delete_torrents(self, hashes, delete_files=True):
        FakeQbt.present = False
        shutil.rmtree(FakeQbt.payload, ignore_errors=True)

    async def close(self):
        pass


@pytest.fixture
async def env(maker, tmp_path, monkeypatch):
    payload = tmp_path / "downloads" / NAME
    payload.mkdir(parents=True)
    with zipfile.ZipFile(payload / "Test Series - c001.cbz", "w") as zf:
        zf.writestr("001.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    (tmp_path / "library").mkdir()
    async with maker() as session:
        await settings_service.set_many(session, {"qbittorrent_enabled": "true"})
        series = Series(title="Test Series", sort_title="test series",
                        root_folder=RootFolder(path=str(tmp_path / "library")),
                        folder_name="Test Series")
        series.chapters = [Chapter(number=1.0, downloaded=False)]
        session.add(series)
        await session.commit()
        dl = Download(series_id=series.id, kind=DownloadKind.TORRENT,
                      status=DownloadStatus.DOWNLOADING, title=NAME, source_name="nyaa",
                      payload="magnet:?xt=urn:btih:" + HASH, torrent_hash=HASH)
        session.add(dl)
        await session.commit()
        dl_id = dl.id

    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session

    imports = []
    real_import = tasks._import_torrent

    async def recording_import(session, dl, *args, **kwargs):
        async with maker() as other:  # the committed status, as the API sees it
            imports.append((dl.id, (await other.get(Download, dl.id)).status))
        await real_import(session, dl, *args, **kwargs)

    FakeQbt.reset(payload)
    monkeypatch.setattr(tasks, "session_scope", scope)
    monkeypatch.setattr(tasks, "QbtClient", FakeQbt)
    monkeypatch.setattr(queue_api, "QbtClient", FakeQbt)
    monkeypatch.setattr(tasks, "_import_torrent", recording_import)
    monkeypatch.setattr(tasks.notifications, "notify_import", lambda *a: None)
    monkeypatch.setattr(tasks, "_notify_kavita", lambda *a: None)
    return dl_id, payload, imports


async def remove_while_held(maker, dl_id):
    """Run a sync pass and, while it is inside the held qBittorrent call,
    remove the download from the queue in the API's own session."""
    sync = asyncio.create_task(tasks.sync_qbittorrent())
    await FakeQbt.entered.wait()
    async with maker() as api_session:
        assert await queue_api._remove_downloads(api_session, [dl_id]) == 1
    FakeQbt.release.set()
    await sync


async def row(maker, dl_id):
    async with maker() as session:
        dl = await session.get(Download, dl_id)
        return dl.status, dl.error


async def queue_ids(maker):
    async with maker() as session:
        return [item.id for item in await queue_api.get_queue(session)]


async def test_removal_while_fetching_torrent_files_is_not_imported(maker, env):
    dl_id, payload, imports = env
    FakeQbt.reset(payload, hold="torrent_files")

    await remove_while_held(maker, dl_id)

    assert await row(maker, dl_id) == (DownloadStatus.FAILED, tasks.REMOVED_BY_USER)
    assert dl_id not in await queue_ids(maker)
    assert imports == []
    # and later passes leave it alone
    for _ in range(tasks.TORRENT_MISSING_LIMIT):
        await tasks.sync_qbittorrent()
    assert await row(maker, dl_id) == (DownloadStatus.FAILED, tasks.REMOVED_BY_USER)
    assert imports == []


async def test_removal_on_the_last_missing_pass_is_not_overwritten(maker, env):
    dl_id, payload, imports = env
    FakeQbt.reset(payload, present=False)  # gone from qBittorrent
    for _ in range(tasks.TORRENT_MISSING_LIMIT - 1):
        await tasks.sync_qbittorrent()
    assert await row(maker, dl_id) == (DownloadStatus.DOWNLOADING, "")

    # the user removes it during the pass that would give up on it
    FakeQbt.hold = "get_torrent"
    await remove_while_held(maker, dl_id)

    # still a removal, not a "vanished" failure that blocks the chapter
    assert await row(maker, dl_id) == (DownloadStatus.FAILED, tasks.REMOVED_BY_USER)
    assert dl_id not in await queue_ids(maker)
    assert imports == []


async def test_finished_torrent_still_moves_to_importing_and_done(maker, env, tmp_path):
    dl_id, payload, imports = env

    await tasks.sync_qbittorrent()

    assert imports == [(dl_id, DownloadStatus.IMPORTING)]
    assert await row(maker, dl_id) == (DownloadStatus.DONE, "")
    async with maker() as session:
        chapter = await session.scalar(select(Chapter))
    assert chapter.downloaded
    assert chapter.file_path.startswith(str(tmp_path / "library" / "Test Series"))


async def test_torrent_missing_past_the_limit_still_fails(maker, env):
    dl_id, payload, imports = env
    FakeQbt.reset(payload, present=False)

    for _ in range(tasks.TORRENT_MISSING_LIMIT):
        await tasks.sync_qbittorrent()

    status, error = await row(maker, dl_id)
    assert status == DownloadStatus.FAILED
    assert "torrent not found in qBittorrent" in error
    assert imports == []
