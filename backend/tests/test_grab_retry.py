"""Monitor grab behavior around failed/removed downloads and chapter-list
caching, against a real (in-memory) database."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from mangarr.jobs import tasks
from mangarr.jobs.tasks import (
    REMOVED_BY_USER,
    auto_grab_best_torrent,
    grab_missing_chapters,
    update_chapters,
)
from mangarr.models import (
    Base,
    Chapter,
    Download,
    DownloadKind,
    DownloadStatus,
    RootFolder,
    Series,
    SeriesSourceLink,
)
from mangarr.sources.base import DirectSource, SourceChapter, TorrentRelease
from mangarr.torrent_selection import TorrentSelection


class FakeSource(DirectSource):
    name = "fake"

    def __init__(self, numbers):
        self.numbers = numbers
        self.list_calls = 0

    async def search_series(self, query):
        return []

    async def list_chapters(self, external_id):
        self.list_calls += 1
        return [
            SourceChapter(source_name=self.name, external_id=f"c{n}", number=float(n))
            for n in self.numbers
        ]

    async def get_pages(self, chapter_external_id):
        return []


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _make_series(session, chapter_numbers):
    series = Series(title="Test Series", sort_title="test series")
    series.source_links.append(
        SeriesSourceLink(source_name="fake", external_id="x")
    )
    series.chapters.extend(
        Chapter(number=float(n), monitored=True) for n in chapter_numbers
    )
    session.add(series)
    await session.commit()
    # reload with eager-loaded relationships, the way _load_series does — an
    # async session cannot lazy-load on attribute access
    result = await session.execute(
        select(Series)
        .options(selectinload(Series.chapters), selectinload(Series.source_links),
                 selectinload(Series.root_folder), selectinload(Series.extra_folders))
        .where(Series.id == series.id)
    )
    return result.scalar_one()


def _use_source(monkeypatch, source):
    monkeypatch.setattr(
        tasks.registry, "enabled_direct_sources", lambda values: [source]
    )


async def test_grabs_missing_chapters(db_session, monkeypatch):
    series = await _make_series(db_session, [1, 2])
    source = FakeSource([1, 2])
    _use_source(monkeypatch, source)

    queued = await grab_missing_chapters(db_session, series, {})

    assert queued == 2


async def test_excluded_chapters_are_not_grabbed(db_session, monkeypatch):
    series = await _make_series(db_session, [1, 2])
    series.chapters[0].excluded = True
    await db_session.commit()
    source = FakeSource([1, 2])
    _use_source(monkeypatch, source)

    queued = await grab_missing_chapters(db_session, series, {})

    assert queued == 1
    direct = (
        await db_session.execute(
            select(Download).where(Download.kind == DownloadKind.DIRECT)
        )
    ).scalar_one()
    assert direct.chapter_id == series.chapters[1].id


async def test_recent_failure_blocks_the_source(db_session, monkeypatch):
    series = await _make_series(db_session, [1])
    source = FakeSource([1])
    _use_source(monkeypatch, source)
    db_session.add(Download(
        series_id=series.id, chapter_id=series.chapters[0].id,
        kind=DownloadKind.DIRECT, status=DownloadStatus.FAILED,
        source_name="fake", error="page 3 failed",
    ))
    await db_session.commit()

    assert await grab_missing_chapters(db_session, series, {}) == 0


async def test_user_removal_does_not_block_the_source(db_session, monkeypatch):
    """Cancelling a queued grab must not blacklist that (chapter, source)."""
    series = await _make_series(db_session, [1])
    source = FakeSource([1])
    _use_source(monkeypatch, source)
    db_session.add(Download(
        series_id=series.id, chapter_id=series.chapters[0].id,
        kind=DownloadKind.DIRECT, status=DownloadStatus.FAILED,
        source_name="fake", error=REMOVED_BY_USER,
    ))
    await db_session.commit()

    assert await grab_missing_chapters(db_session, series, {}) == 1


async def test_old_failure_is_retried(db_session, monkeypatch):
    series = await _make_series(db_session, [1])
    source = FakeSource([1])
    _use_source(monkeypatch, source)
    stale = datetime.now(timezone.utc) - tasks.FAILED_GRAB_RETRY_AFTER - timedelta(days=1)
    db_session.add(Download(
        series_id=series.id, chapter_id=series.chapters[0].id,
        kind=DownloadKind.DIRECT, status=DownloadStatus.FAILED,
        source_name="fake", error="page 3 failed",
        created_at=stale, updated_at=stale,
    ))
    await db_session.commit()

    assert await grab_missing_chapters(db_session, series, {}) == 1


async def test_series_level_download_still_blocks_grabs(db_session, monkeypatch):
    series = await _make_series(db_session, [1])
    source = FakeSource([1])
    _use_source(monkeypatch, source)
    db_session.add(Download(
        series_id=series.id, chapter_id=None,
        kind=DownloadKind.TORRENT, status=DownloadStatus.DOWNLOADING,
        source_name="nyaa",
    ))
    await db_session.commit()

    assert await grab_missing_chapters(db_session, series, {}) == 0


async def test_empty_exclusions_do_not_bypass_series_download_guard(db_session, monkeypatch):
    series = await _make_series(db_session, [1])
    source = FakeSource([1])
    _use_source(monkeypatch, source)
    db_session.add(Download(
        series_id=series.id,
        chapter_id=None,
        kind=DownloadKind.TORRENT,
        status=DownloadStatus.DOWNLOADING,
        source_name="nyaa",
    ))
    await db_session.commit()

    assert await grab_missing_chapters(
        db_session, series, {}, exclude_numbers=set()
    ) == 0


async def test_chapter_cache_shared_between_update_and_grab(db_session, monkeypatch):
    """One monitor pass must fetch each source's chapter list once, not once
    per consumer."""
    series = await _make_series(db_session, [])
    source = FakeSource([1, 2])
    _use_source(monkeypatch, source)
    # keep update_chapters off the network beyond the fake source
    series.mangaupdates_id = None

    cache = {}
    added = await update_chapters(db_session, series, {}, cache)
    queued = await grab_missing_chapters(db_session, series, {}, chapter_cache=cache)

    assert added == 2
    assert queued == 2
    assert source.list_calls == 1
    assert all(ch.available_sources == "fake" for ch in series.chapters)


async def test_unrefreshed_chapter_availability_is_unknown(db_session):
    series = await _make_series(db_session, [1])
    assert series.chapters[0].available_sources is None


async def test_successful_refresh_clears_stale_source_availability(db_session, monkeypatch):
    series = await _make_series(db_session, [1, 2])
    for chapter in series.chapters:
        chapter.available_sources = "fake"
    await db_session.commit()
    source = FakeSource([1])
    _use_source(monkeypatch, source)

    await update_chapters(db_session, series, {})

    assert {ch.number: ch.available_sources for ch in series.chapters} == {
        1.0: "fake",
        2.0: "",
    }


async def test_refresh_does_not_mutate_excluded_chapter(db_session, monkeypatch):
    series = await _make_series(db_session, [1])
    chapter = series.chapters[0]
    chapter.excluded = True
    chapter.title = "Keep me"
    chapter.volume = 7
    chapter.available_sources = "old-source"
    await db_session.commit()
    source = FakeSource([1])
    _use_source(monkeypatch, source)

    await update_chapters(db_session, series, {})

    assert chapter.title == "Keep me"
    assert chapter.volume == 7
    assert chapter.available_sources == "old-source"


async def test_refresh_clears_availability_from_disabled_sources(db_session, monkeypatch):
    series = await _make_series(db_session, [1])
    series.mangaupdates_id = None
    series.chapters[0].available_sources = "fake"
    await db_session.commit()
    monkeypatch.setattr(tasks.registry, "enabled_direct_sources", lambda values: [])

    await update_chapters(db_session, series, {})

    assert series.chapters[0].available_sources == ""


async def test_new_torrent_coverage_allows_uncovered_direct_grabs(db_session, monkeypatch):
    series = await _make_series(db_session, [1, 2])
    source = FakeSource([1, 2])
    _use_source(monkeypatch, source)
    torrent = Download(
        series_id=series.id,
        chapter_id=None,
        kind=DownloadKind.TORRENT,
        status=DownloadStatus.DOWNLOADING,
        source_name="nyaa",
    )
    db_session.add(torrent)
    await db_session.commit()

    queued = await grab_missing_chapters(
        db_session,
        series,
        {},
        exclude_numbers={1.0},
        allowed_series_download_id=torrent.id,
    )

    assert queued == 1
    direct = (
        await db_session.execute(
            select(Download).where(Download.kind == DownloadKind.DIRECT)
        )
    ).scalar_one()
    assert direct.chapter_id == next(ch.id for ch in series.chapters if ch.number == 2.0)


async def test_auto_torrent_grab_returns_inspected_coverage(db_session, tmp_path, monkeypatch):
    series = await _make_series(db_session, [1, 2.5])
    series.root_folder = RootFolder(path=str(tmp_path))
    await db_session.commit()
    release = TorrentRelease(
        source_name="nyaa",
        title="Test Series pack",
        magnet="magnet:?xt=urn:btih:" + "a" * 40,
        size_bytes=1024,
        seeders=4,
    )
    captured = {}

    async def fake_select(*args, **kwargs):
        captured["max_size"] = kwargs["max_size_bytes"]
        captured["min_seeders"] = kwargs["min_seeders"]
        return TorrentSelection(release=release, coverage={1.0, 2.5})

    async def fake_enqueue(session, selected_series, magnet, title, values):
        captured["enqueued"] = (selected_series.id, magnet, title)
        return SimpleNamespace(id=321)

    monkeypatch.setattr(tasks, "select_best_torrent", fake_select)
    monkeypatch.setattr(tasks, "enqueue_torrent", fake_enqueue)
    monkeypatch.setattr(tasks.registry, "enabled_torrent_indexers", lambda values: [object()])

    grab = await auto_grab_best_torrent(
        db_session,
        series,
        {
            "qbittorrent_enabled": "true",
            "torrent_auto_max_size_gib": "30",
            "torrent_auto_min_seeders": "1",
        },
    )

    assert grab is not None
    assert grab.download_id == 321
    assert grab.coverage == {1.0, 2.5}
    assert captured["max_size"] == 30 * 1024**3
    assert captured["min_seeders"] == 1
    assert captured["enqueued"] == (series.id, release.magnet, release.title)


async def test_auto_torrent_skips_when_series_download_is_active(
    db_session, tmp_path, monkeypatch
):
    series = await _make_series(db_session, [1])
    series.root_folder = RootFolder(path=str(tmp_path))
    db_session.add(Download(
        series_id=series.id,
        chapter_id=None,
        kind=DownloadKind.TORRENT,
        status=DownloadStatus.DOWNLOADING,
        source_name="nyaa",
    ))
    await db_session.commit()
    monkeypatch.setattr(tasks.registry, "enabled_torrent_indexers", lambda values: [object()])

    async def unexpected_select(*args, **kwargs):
        raise AssertionError("selection must not run while a series torrent is active")

    monkeypatch.setattr(tasks, "select_best_torrent", unexpected_select)

    assert await auto_grab_best_torrent(
        db_session, series, {"qbittorrent_enabled": "true"}
    ) is None


async def test_active_direct_download_removed_by_user_is_not_completed(
    db_session, tmp_path, monkeypatch
):
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
    )
    db_session.add(dl)
    await db_session.commit()
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource([1]))

    async def fake_download(source, payload, series, chapter, dest, progress_cb=None,
                            cancel_cb=None, web_url=""):
        dl.status = DownloadStatus.FAILED
        dl.error = REMOVED_BY_USER
        await db_session.commit()
        if cancel_cb is not None:
            await cancel_cb()
        raise AssertionError("cancel callback should abort the download")

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", fake_download)

    await tasks._run_direct_download(db_session, dl)
    await db_session.refresh(dl)
    await db_session.refresh(chapter)

    assert dl.status == DownloadStatus.FAILED
    assert dl.error == REMOVED_BY_USER
    assert chapter.downloaded is False


async def test_queued_direct_download_is_stopped_after_chapter_exclusion(
    db_session, tmp_path, monkeypatch
):
    root = RootFolder(path=str(tmp_path))
    series = Series(
        title="Test Series",
        sort_title="test series",
        root_folder=root,
        folder_name="Test Series",
    )
    chapter = Chapter(number=1.0, monitored=True, excluded=True)
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
    )
    db_session.add(dl)
    await db_session.commit()
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource([1]))

    await tasks._run_direct_download(db_session, dl)

    assert dl.status == DownloadStatus.FAILED
    assert "excluded" in dl.error
    assert chapter.downloaded is False
