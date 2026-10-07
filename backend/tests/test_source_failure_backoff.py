"""A source that fails with a transient error (down, timing out, 429/5xx) is
left alone for a cooldown instead of being asked again — with its full
request back-off — for every series in the monitor pass."""

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.jobs import tasks
from mangarr.models import Base, Chapter, Series, SeriesSourceLink
from mangarr.sources.base import DirectSource

COOLDOWN = 600.0


class FailingSource(DirectSource):
    name = "fake"

    def __init__(self, error):
        self.error = error
        self.list_calls = 0
        self.search_calls = 0

    async def search_series(self, query):
        self.search_calls += 1
        raise self.error

    async def list_chapters(self, external_id):
        self.list_calls += 1
        raise self.error

    async def get_pages(self, chapter_external_id):
        return []


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


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


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(tasks, "time", clock)
    monkeypatch.setattr(tasks, "_link_retry_at", {})
    monkeypatch.setattr(tasks, "_source_retry_at", {}, raising=False)
    monkeypatch.setattr(tasks, "SOURCE_COOLDOWN_SECONDS", COOLDOWN, raising=False)
    return clock


def _use_source(monkeypatch, source):
    monkeypatch.setattr(tasks.registry, "enabled_direct_sources", lambda values: [source])


async def _make_series(session, title, external_id=None):
    series = Series(title=title, sort_title=title.lower())
    if external_id is not None:
        series.source_links.append(SeriesSourceLink(source_name="fake", external_id=external_id))
    series.chapters.append(Chapter(number=1.0, monitored=True))
    session.add(series)
    await session.commit()
    return await tasks._load_series(session, series.id)


def _http_error(status):
    request = httpx.Request("GET", "https://src.test/list")
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=httpx.Response(status, request=request)
    )


@pytest.mark.parametrize("error", [
    httpx.ConnectError("connection refused"),
    httpx.ReadTimeout("timed out"),
    _http_error(503),
], ids=["connect-error", "read-timeout", "http-503"])
async def test_dead_source_is_asked_once_per_cooldown_across_series(
    db_session, monkeypatch, clock, error
):
    source = FailingSource(error)
    _use_source(monkeypatch, source)
    first = await _make_series(db_session, "First", "a")
    second = await _make_series(db_session, "Second", "b")

    # one monitor pass: refresh + grab for each series
    for series in (first, second):
        cache = {}
        await tasks.update_chapters(db_session, series, {}, cache)
        assert await tasks.grab_missing_chapters(
            db_session, series, {}, chapter_cache=cache
        ) == 0
    assert source.list_calls == 1

    clock.now += COOLDOWN + 1  # cooled off: probe the source again
    await tasks.update_chapters(db_session, second, {}, {})
    assert source.list_calls == 2


async def test_cooling_source_keeps_its_chapter_availability(
    db_session, monkeypatch, clock
):
    source = FailingSource(httpx.ConnectError("connection refused"))
    _use_source(monkeypatch, source)
    first = await _make_series(db_session, "First", "a")
    second = await _make_series(db_session, "Second", "b")
    second.chapters[0].available_sources = "fake"
    await db_session.commit()

    await tasks.update_chapters(db_session, first, {}, {})
    await tasks.update_chapters(db_session, second, {}, {})  # skipped, not failed over

    assert second.chapters[0].available_sources == "fake"


async def test_permanent_list_failure_does_not_silence_the_source(
    db_session, monkeypatch, clock
):
    source = FailingSource(ValueError("unexpected payload"))
    _use_source(monkeypatch, source)
    first = await _make_series(db_session, "First", "a")
    second = await _make_series(db_session, "Second", "b")

    await tasks.update_chapters(db_session, first, {}, {})
    await tasks.update_chapters(db_session, second, {}, {})

    assert source.list_calls == 2  # one series' broken listing says nothing about another's


async def test_failed_search_backs_off_the_source_for_every_series(
    db_session, monkeypatch, clock
):
    source = FailingSource(httpx.ConnectError("connection refused"))
    _use_source(monkeypatch, source)
    first = await _make_series(db_session, "First")
    second = await _make_series(db_session, "Second")

    await tasks.link_sources(db_session, first, {}, respect_backoff=True)
    await tasks.link_sources(db_session, second, {}, respect_backoff=True)
    await tasks.link_sources(db_session, first, {}, respect_backoff=True)
    assert source.search_calls == 1

    clock.now += COOLDOWN + 1  # the source may be back: the next pass searches
    await tasks.link_sources(db_session, first, {}, respect_backoff=True)
    assert source.search_calls == 2


async def test_broken_search_waits_like_no_match(db_session, monkeypatch, clock):
    source = FailingSource(ValueError("unexpected search page"))
    _use_source(monkeypatch, source)
    first = await _make_series(db_session, "First")
    second = await _make_series(db_session, "Second")

    await tasks.link_sources(db_session, first, {}, respect_backoff=True)
    clock.now += 3600  # next pass
    await tasks.link_sources(db_session, first, {}, respect_backoff=True)
    assert source.search_calls == 1

    # not a dead source, so other series still search it
    await tasks.link_sources(db_session, second, {}, respect_backoff=True)
    assert source.search_calls == 2

    clock.now += tasks.LINK_RETRY_AFTER_SECONDS
    await tasks.link_sources(db_session, first, {}, respect_backoff=True)
    assert source.search_calls == 3
