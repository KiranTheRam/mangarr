"""A direct download the worker fails without asking its source — the source
is disabled, or the series has no root folder — must not block that source
once the cause is fixed, and a series without a root folder isn't grabbed."""

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr import settings_service
from mangarr.api.system import delete_root_folder
from mangarr.jobs import tasks
from mangarr.models import (
    Base,
    Chapter,
    Download,
    DownloadStatus,
    RootFolder,
    Series,
    SeriesSourceLink,
)
from mangarr.sources.base import DirectSource, SourceChapter


class FakeSource(DirectSource):
    name = "fake"

    async def search_series(self, query):
        return []

    async def list_chapters(self, external_id):
        return [SourceChapter(source_name=self.name, external_id="c1", number=1.0)]

    async def get_pages(self, chapter_external_id):
        return []


class OtherSource(FakeSource):
    name = "other"


@pytest.fixture
async def db_session(monkeypatch):
    monkeypatch.setitem(settings_service.DEFAULTS, "source_fake_enabled", "true")
    monkeypatch.setitem(settings_service.DEFAULTS, "source_other_enabled", "true")
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource())
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "other", OtherSource())
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _series(session, root: Path | None, sources=("fake",)) -> int:
    """A series wanting chapter 1, linked to `sources` (best first)."""
    series = Series(title="Series", sort_title="series", folder_name="Series")
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
        series.root_folder = RootFolder(path=str(root))
    for name in sources:
        series.source_links.append(SeriesSourceLink(source_name=name, external_id="x"))
    series.chapters.append(Chapter(number=1.0, monitored=True))
    session.add(series)
    await session.commit()
    return series.id


async def _monitor_pass(session, series_id) -> list[Download]:
    """Grab for the series as the monitor does; return the downloads queued."""
    values = await tasks.registry.apply_settings(session)
    series = await tasks._load_series(session, series_id)
    await tasks.grab_missing_chapters(session, series, values)
    return list((await session.execute(
        select(Download).where(Download.status == DownloadStatus.QUEUED)
    )).scalars())


async def _run(session, dl) -> Download:
    await tasks._run_direct_download(session, dl)
    await session.refresh(dl)
    return dl


async def test_a_disabled_source_does_not_block_once_enabled_again(
    db_session, tmp_path
):
    sid = await _series(db_session, tmp_path / "manga")
    [dl] = await _monitor_pass(db_session, sid)

    # the user disables the source while the chapter is still queued
    await settings_service.set_many(db_session, {"source_fake_enabled": "false"})
    dl = await _run(db_session, dl)
    assert dl.status == DownloadStatus.FAILED
    assert dl.error == "source is disabled; enable it before retrying"

    await settings_service.set_many(db_session, {"source_fake_enabled": "true"})
    retry = await _monitor_pass(db_session, sid)
    assert [(r.chapter_id, r.source_name) for r in retry] == [(dl.chapter_id, "fake")]


async def test_a_deleted_root_folder_does_not_block_once_a_root_is_set(
    db_session, tmp_path
):
    sid = await _series(db_session, tmp_path / "manga")
    [dl] = await _monitor_pass(db_session, sid)

    # the user deletes the root folder while the chapter is still queued
    dl_id = dl.id
    series = await tasks._load_series(db_session, sid)
    await delete_root_folder(series.root_folder_id, db_session)
    # the worker runs in its own session, which sees the series without a root
    db_session.expire_all()
    dl = await _run(db_session, await db_session.get(Download, dl_id))
    assert dl.status == DownloadStatus.FAILED
    assert dl.error == "series has no root folder configured"

    # ... and gives the series a new one
    (tmp_path / "manga2").mkdir()
    series = await tasks._load_series(db_session, sid)
    series.root_folder = RootFolder(path=str(tmp_path / "manga2"))
    await db_session.commit()
    retry = await _monitor_pass(db_session, sid)
    assert [(r.chapter_id, r.source_name) for r in retry] == [(dl.chapter_id, "fake")]


async def test_a_series_without_a_root_folder_is_not_grabbed(db_session):
    # every pass used to queue the chapter on the next linked source, where
    # the worker failed it, until every source was blocked
    sid = await _series(db_session, None, sources=("fake", "other"))
    for _ in range(3):
        assert await _monitor_pass(db_session, sid) == []
    assert (await db_session.execute(select(Download))).scalars().all() == []


@pytest.mark.parametrize("error", [
    "page 3 failed",
    "fake returned no pages for chapter 1",
    "the source is disabled upstream",  # not one of the worker's prefixes
])
async def test_a_source_failure_still_blocks(db_session, tmp_path, error):
    sid = await _series(db_session, tmp_path / "manga")
    [dl] = await _monitor_pass(db_session, sid)
    dl.status = DownloadStatus.FAILED
    dl.error = error
    await db_session.commit()

    assert await _monitor_pass(db_session, sid) == []
