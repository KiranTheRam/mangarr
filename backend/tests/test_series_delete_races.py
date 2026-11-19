"""Series deletion must not interleave with archive writes that validated the series."""

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from mangarr.api import issues as issues_api, series as series_api
from mangarr.comicinfo import read_comicinfo
from mangarr.models import Series
from mangarr.schemas import ComicInfoUpdateIn, IssueResolveIn, SeriesBulkIn
from tests.test_metadata_issues import repair_db  # noqa: F401


async def _delete(maker, series_id, bulk):
    async with maker() as session:
        if bulk:
            await series_api.delete_series_bulk(SeriesBulkIn(series_ids=[series_id]), session)
        else:
            await series_api.delete_series(series_id, session)


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("operation", ["resolve", "edit"])
async def test_delete_waits_for_an_in_flight_archive_write(repair_db, monkeypatch, bulk, operation):
    session, maker, series, path = repair_db
    await issues_api.scan_issues(session)
    [issue] = await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    current = await issues_api.get_chapter_comicinfo(series.id, series.chapters[0].id, session)
    validated, release = asyncio.Event(), asyncio.Event()
    write = issues_api._write_archive

    async def pause_after_validation(*args, **kwargs):
        validated.set()
        await release.wait()
        return await write(*args, **kwargs)

    monkeypatch.setattr(issues_api, "_write_archive", pause_after_validation)

    async def repair():
        async with maker() as repair_session:
            if operation == "resolve":
                await issues_api.resolve_issue(issue.id, IssueResolveIn(revision=issue.revision), repair_session)
            else:
                await issues_api.update_chapter_comicinfo(
                    series.id, series.chapters[0].id,
                    ComicInfoUpdateIn(revision=current.revision, volume=1), repair_session,
                )

    async with asyncio.timeout(10):
        repairing = asyncio.create_task(repair())
        await validated.wait()
        deleting = asyncio.create_task(_delete(maker, series.id, bulk))
        try:
            done, _ = await asyncio.wait({deleting}, timeout=0.05)
            assert not done, "deletion committed while a repair still held the series"
        finally:
            release.set()
            await asyncio.gather(repairing, deleting)

    assert read_comicinfo(path).volume == "1"
    async with maker() as check:
        assert await check.scalar(select(Series.id).where(Series.id == series.id)) is None


async def test_repair_queued_behind_delete_does_not_touch_the_archive(repair_db):
    session, maker, series, path = repair_db
    await issues_api.scan_issues(session)
    [issue] = await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    before = path.read_bytes()
    lock = series_api.series_lock(series.id)
    await lock.acquire()

    async def repair():
        async with maker() as repair_session:
            await issues_api.resolve_issue(issue.id, IssueResolveIn(revision=issue.revision), repair_session)

    async with asyncio.timeout(10):
        repairing = asyncio.create_task(repair())
        deleting = asyncio.create_task(_delete(maker, series.id, False))
        await asyncio.sleep(0)
        lock.release()
        results = await asyncio.gather(repairing, deleting, return_exceptions=True)

    assert isinstance(results[0], HTTPException) and results[0].status_code == 404
    assert results[1] is None
    assert path.read_bytes() == before
