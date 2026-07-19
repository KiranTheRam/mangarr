"""Interactive search can be scoped to a set of chapters, so a batch of
missing chapters is searched in one pass instead of one modal at a time."""

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.api import search
from mangarr.models import Base, Chapter, Series, SeriesSourceLink
from mangarr.sources.base import SourceChapter, TorrentRelease


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


class FakeDirectSource:
    name = "fake"

    def __init__(self, numbers):
        self.numbers = numbers

    async def list_chapters(self, external_id):
        return [
            SourceChapter(source_name=self.name, external_id=f"c{n:g}", number=n)
            for n in self.numbers
        ]


class FakeIndexer:
    name = "fake-tracker"

    async def search(self, query):
        return [TorrentRelease(source_name=self.name, title="Vol 1-10 pack", magnet="magnet:?x")]


@pytest.fixture
def fake_sources(monkeypatch):
    """Serve chapters 1–4 from one direct source plus one torrent indexer."""
    source = FakeDirectSource([1.0, 2.0, 3.0, 4.0])

    async def apply_settings(session):
        return {"qbittorrent_enabled": "true"}

    monkeypatch.setattr(search.registry, "apply_settings", apply_settings)
    monkeypatch.setattr(search.registry, "enabled_direct_sources", lambda values: [source])
    monkeypatch.setattr(search.registry, "enabled_torrent_indexers", lambda values: [FakeIndexer()])
    return source


async def _series(session, downloaded_numbers=()):
    series = Series(title="Test Series", sort_title="test series")
    for n in (1.0, 2.0, 3.0, 4.0):
        series.chapters.append(Chapter(number=n, downloaded=n in downloaded_numbers))
    series.source_links.append(
        SeriesSourceLink(source_name="fake", external_id="ext-1", external_title="Test Series")
    )
    session.add(series)
    await session.commit()
    return series


def _numbers(releases):
    return sorted(r.chapter_number for r in releases if r.kind == "direct")


async def test_series_wide_search_returns_only_missing_chapters(db_session, fake_sources):
    series = await _series(db_session, downloaded_numbers=(1.0, 3.0))

    releases = await search.search_releases(series_id=series.id, session=db_session)

    assert _numbers(releases) == [2.0, 4.0]


async def test_scoped_search_returns_exactly_the_requested_chapters(db_session, fake_sources):
    series = await _series(db_session)
    wanted = [c.id for c in series.chapters if c.number in (2.0, 4.0)]

    releases = await search.search_releases(chapter_id=wanted, session=db_session)

    assert _numbers(releases) == [2.0, 4.0]
    # every direct result carries its own chapter, so the UI can grab a batch
    assert {r.chapter_id for r in releases if r.kind == "direct"} == set(wanted)


async def test_scoped_search_includes_already_downloaded_chapters(db_session, fake_sources):
    """Picking a chapter you already have is a deliberate re-grab, unlike the
    series-wide search which is about filling gaps."""
    series = await _series(db_session, downloaded_numbers=(2.0,))
    downloaded = next(c.id for c in series.chapters if c.number == 2.0)

    releases = await search.search_releases(chapter_id=[downloaded], session=db_session)

    assert _numbers(releases) == [2.0]


async def test_torrents_can_be_dropped_from_a_scoped_search(db_session, fake_sources):
    series = await _series(db_session)
    wanted = [c.id for c in series.chapters if c.number == 2.0]

    with_torrents = await search.search_releases(chapter_id=wanted, session=db_session)
    without = await search.search_releases(
        chapter_id=wanted, include_torrents=False, session=db_session
    )

    assert any(r.kind == "torrent" for r in with_torrents)
    assert all(r.kind == "direct" for r in without)
    assert _numbers(without) == [2.0]


async def test_unknown_chapter_is_rejected(db_session, fake_sources):
    series = await _series(db_session)
    good = series.chapters[0].id

    with pytest.raises(HTTPException) as exc:
        await search.search_releases(chapter_id=[good, 9999], session=db_session)

    assert exc.value.status_code == 404


async def test_chapters_from_two_series_are_rejected(db_session, fake_sources):
    first = await _series(db_session)
    second = await _series(db_session)

    with pytest.raises(HTTPException) as exc:
        await search.search_releases(
            chapter_id=[first.chapters[0].id, second.chapters[0].id], session=db_session
        )

    assert exc.value.status_code == 422


async def test_search_without_a_target_is_rejected(db_session, fake_sources):
    with pytest.raises(HTTPException) as exc:
        await search.search_releases(session=db_session)

    assert exc.value.status_code == 422
