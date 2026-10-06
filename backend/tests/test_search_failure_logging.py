"""A source that errors (blocked, Cloudflare-challenged, down) still yields
no results to the caller, but the failure must reach the log; otherwise it
looks exactly like a search that found nothing."""

import logging

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.api import search
from mangarr.models import Base, Chapter, Series, SeriesSourceLink
from mangarr.sources.base import TorrentIndexer
from mangarr.torrent_selection import select_best_torrent


def _blocked(url: str):
    """Raise what a Cloudflare challenge page looks like to httpx."""
    httpx.Response(403, request=httpx.Request("GET", url)).raise_for_status()


class BlockedDirectSource:
    def __init__(self, name):
        self.name = name

    async def search_series(self, query):
        _blocked(f"https://{self.name}.example/search")

    async def list_chapters(self, external_id):
        _blocked(f"https://{self.name}.example/series/{external_id}")


class BlockedIndexer(TorrentIndexer):
    name = "blocked-tracker"

    async def search(self, query):
        _blocked("https://tracker.example/search")


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def blocked_sources(monkeypatch):
    linked = BlockedDirectSource("linked-src")
    unlinked = BlockedDirectSource("lookup-src")

    async def apply_settings(session):
        return {"qbittorrent_enabled": "true"}

    monkeypatch.setattr(search.registry, "apply_settings", apply_settings)
    monkeypatch.setattr(
        search.registry, "enabled_direct_sources", lambda values: [linked, unlinked]
    )
    monkeypatch.setattr(
        search.registry, "enabled_torrent_indexers", lambda values: [BlockedIndexer()]
    )


async def _series(session):
    series = Series(title="Test Series", sort_title="test series")
    series.chapters.append(Chapter(number=1.0))
    series.source_links.append(
        SeriesSourceLink(
            source_name="linked-src", external_id="ext-1", external_title="Test Series"
        )
    )
    session.add(series)
    await session.commit()
    return series


def _warnings(caplog, needle):
    return [
        r for r in caplog.records
        if r.levelno == logging.WARNING and needle in r.getMessage()
    ]


async def test_release_search_logs_each_failing_source(db_session, blocked_sources, caplog):
    series = await _series(db_session)

    with caplog.at_level(logging.WARNING, logger="mangarr.api.search"):
        releases = await search.search_releases(series_id=series.id, session=db_session)

    assert releases == []  # the response is unchanged: failures still yield nothing
    chapters = _warnings(caplog, "linked-src")
    assert len(chapters) == 1, "a linked source's failed chapter list was not logged"
    assert "ext-1" in chapters[0].getMessage() and "403" in chapters[0].getMessage()
    lookup = _warnings(caplog, "lookup-src")
    assert len(lookup) == 1, "an unlinked source's failed title search was not logged"
    assert "Test Series" in lookup[0].getMessage() and "403" in lookup[0].getMessage()
    tracker = _warnings(caplog, "blocked-tracker")
    assert tracker, "a failed torrent indexer search was not logged"
    assert all("403" in r.getMessage() for r in tracker)


async def test_source_id_lookup_logs_the_failure(caplog):
    with caplog.at_level(logging.WARNING, logger="mangarr.api.search"):
        matches = await search._find_direct_source_ids(
            BlockedDirectSource("lookup-src"), ["Test Series"]
        )

    assert matches == []
    records = _warnings(caplog, "lookup-src")
    assert len(records) == 1, "the failed source search was not logged"
    assert "HTTPStatusError" in records[0].getMessage()


async def test_torrent_selection_logs_a_failing_indexer(caplog):
    series = Series(title="Test Series", sort_title="test series")
    chapters = [Chapter(number=1.0, downloaded=False)]

    with caplog.at_level(logging.WARNING, logger="mangarr.torrent_selection"):
        selected = await select_best_torrent(
            series, chapters, [BlockedIndexer()], max_size_bytes=1000, min_seeders=1
        )

    assert selected is None
    records = _warnings(caplog, "blocked-tracker")
    assert records, "the failed indexer search was not logged"
    assert all("403" in r.getMessage() for r in records)
