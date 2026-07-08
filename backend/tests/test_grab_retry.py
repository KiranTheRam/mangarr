"""Monitor grab behavior around failed/removed downloads and chapter-list
caching, against a real (in-memory) database."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from mangarr.jobs import tasks
from mangarr.jobs.tasks import REMOVED_BY_USER, grab_missing_chapters, update_chapters
from mangarr.models import (
    Base,
    Chapter,
    Download,
    DownloadKind,
    DownloadStatus,
    Series,
    SeriesSourceLink,
)
from mangarr.sources.base import DirectSource, SourceChapter


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
