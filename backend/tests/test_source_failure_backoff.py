"""A source that fails with a transient error (down, timing out, 429/5xx) is
left alone for a cooldown instead of being asked again — with its full
request back-off — for every series in the monitor pass."""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.jobs import tasks
from mangarr.models import Base, Chapter, Download, Series, SeriesSourceLink
from mangarr.sources.base import DirectSource, SourceChapter

COOLDOWN = 600.0


class FailingSource(DirectSource):
    name = "fake"

    def __init__(self, error):
        self.error = error
        self.list_calls = 0
        self.search_calls = 0
        self.volume_calls = 0
        self.metadata_calls = 0

    async def search_series(self, query):
        self.search_calls += 1
        raise self.error

    async def list_chapters(self, external_id):
        self.list_calls += 1
        raise self.error

    async def get_pages(self, chapter_external_id):
        return []

    async def get_volume_map(self, external_id):
        self.volume_calls += 1
        raise self.error

    async def get_chapter_metadata(self, external_id):
        self.metadata_calls += 1
        raise self.error


class MetadataOnlySource(FailingSource):
    """Like Wikipedia/VIZ: serves no chapters, so only its metadata calls
    can find out that it is down."""

    async def list_chapters(self, external_id):
        self.list_calls += 1
        return []


class ListingSource(FailingSource):
    """Lists chapter 1 fine; only the call named by `failing` errors."""

    def __init__(self, name, failing=None, error=None):
        super().__init__(error)
        self.name = name
        self.failing = failing

    async def list_chapters(self, external_id):
        self.list_calls += 1
        return [SourceChapter(source_name=self.name, external_id=f"{self.name}-1", number=1.0)]

    async def get_volume_map(self, external_id):
        self.volume_calls += 1
        if self.failing == "volume":
            raise self.error
        return {}

    async def get_chapter_metadata(self, external_id):
        self.metadata_calls += 1
        if self.failing == "metadata":
            raise self.error
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


async def _monitor_pass(session, series):
    """What monitor_all() does with a series' sources."""
    cache = {}
    await tasks.link_sources(session, series, {}, respect_backoff=True)
    await tasks.update_chapters(session, series, {}, cache)
    return await tasks.grab_missing_chapters(session, series, {}, chapter_cache=cache)


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


async def test_cooling_source_gets_no_volume_or_metadata_calls(
    db_session, monkeypatch, clock
):
    # a MangaDex-style source: its listing fails, and the series lacks volume
    # data, so update_chapters goes on to ask it for the aggregate
    source = FailingSource(httpx.ConnectError("connection refused"))
    _use_source(monkeypatch, source)
    first = await _make_series(db_session, "First", "a")
    second = await _make_series(db_session, "Second", "b")

    for series in (first, second):
        await _monitor_pass(db_session, series)

    assert source.list_calls == 1
    assert source.volume_calls == 0
    assert source.metadata_calls == 0


async def test_failing_metadata_source_cools_down(db_session, monkeypatch, clock):
    # Wikipedia/VIZ list no chapters, so a dead one is only noticed through
    # its volume-map/metadata calls — which must open the cooldown too
    source = MetadataOnlySource(httpx.ReadTimeout("timed out"))
    _use_source(monkeypatch, source)
    first = await _make_series(db_session, "First", "a")
    second = await _make_series(db_session, "Second", "b")

    for series in (first, second):
        await _monitor_pass(db_session, series)
    assert source.volume_calls + source.metadata_calls == 1

    clock.now += COOLDOWN + 1  # cooled off: probe the source again
    await _monitor_pass(db_session, second)
    assert source.volume_calls + source.metadata_calls == 2


async def test_volume_resync_still_asks_a_cooling_source(
    db_session, monkeypatch, clock
):
    # the resync endpoints pick and overwrite volume data from what
    # collect_volume_maps returns: they must not quietly lose a source
    source = FailingSource(httpx.ConnectError("connection refused"))
    _use_source(monkeypatch, source)
    series = await _make_series(db_session, "First", "a")

    await _monitor_pass(db_session, series)  # opens the cooldown
    calls = source.volume_calls
    await tasks.collect_volume_maps(series, {})
    assert source.volume_calls == calls + 1


async def test_cooling_source_is_logged_once_not_per_series(
    db_session, monkeypatch, clock, caplog
):
    source = FailingSource(httpx.ConnectError("connection refused"))
    _use_source(monkeypatch, source)
    series = [await _make_series(db_session, f"S{i}", str(i)) for i in range(3)]

    caplog.set_level("DEBUG", logger=tasks.log.name)
    await _monitor_pass(db_session, series[0])  # the failure opens the cooldown
    first_pass = list(caplog.records)

    caplog.clear()
    for s in series[1:]:
        await _monitor_pass(db_session, s)
    # skipping a cooling source is not a failure of every series that uses it
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == []
    skipped = [r for r in caplog.records
               if r.levelname == "DEBUG" and "skipped" in r.getMessage()]
    # per series: the listing in update and in grab, and the volume map
    assert len(skipped) == 6

    # the cooldown itself is announced once
    opened = [r for r in first_pass
              if r.levelname == "WARNING" and "skipping it for" in r.getMessage()]
    assert len(opened) == 1


@pytest.mark.parametrize("failing", ["volume", "metadata"])
async def test_source_failing_after_its_listing_is_not_grabbed_from(
    db_session, monkeypatch, clock, failing
):
    # the listing succeeds and is cached for the grab, then the volume-map or
    # metadata call fails transiently: the grab must skip the cooling source
    # and take the chapter from the next one, not queue it on the dead one
    primary = ListingSource("fake", failing, httpx.ConnectError("connection refused"))
    backup = ListingSource("backup")
    monkeypatch.setattr(
        tasks.registry, "enabled_direct_sources", lambda values: [primary, backup]
    )
    series = await _make_series(db_session, "First", "a")
    series.source_links.append(SeriesSourceLink(source_name="backup", external_id="b"))
    await db_session.commit()

    assert await _monitor_pass(db_session, series) == 1

    grabbed = (await db_session.execute(select(Download.source_name))).scalars().all()
    assert grabbed == ["backup"]
    assert primary.list_calls == 1
