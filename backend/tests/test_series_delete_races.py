"""Series deletion must not interleave with archive writes that validated the series,
and must not make the request wait behind a long refresh."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from mangarr import settings_service
from mangarr.api import issues as issues_api, queue, series as series_api, system
from mangarr.comicinfo import read_comicinfo
from mangarr.jobs import tasks
from mangarr.jobs.tasks import REMOVED_BY_USER
from mangarr.models import Chapter, Download, DownloadKind, DownloadStatus, Series
from mangarr.schemas import ComicInfoUpdateIn, GrabIn, IssueResolveIn, SeriesBulkIn
from tests.test_metadata_issues import repair_db  # noqa: F401


async def _delete(maker, series_id, bulk):
    async with maker() as session:
        if bulk:
            await series_api.delete_series_bulk(SeriesBulkIn(series_ids=[series_id]), session)
        else:
            await series_api.delete_series(series_id, session)


async def _series_row(maker, series_id):
    async with maker() as session:
        return await session.scalar(select(Series.id).where(Series.id == series_id))


async def _finish_deletions():
    await asyncio.gather(*list(series_api._deletions.values()))


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("operation", ["resolve", "edit"])
async def test_delete_returns_at_once_and_waits_for_an_in_flight_write(repair_db, monkeypatch, bulk, operation):
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
        try:
            await _delete(maker, series.id, bulk)  # does not wait for the repair
            assert await _series_row(maker, series.id) == series.id, "deleted under a running repair"
            async with maker() as check:
                with pytest.raises(HTTPException) as hidden:
                    await series_api.get_series(series.id, check)
                assert hidden.value.status_code == 404
                assert series.id not in {item.id for item in await series_api.list_series(check)}
        finally:
            release.set()
            await repairing
        await _finish_deletions()

    assert read_comicinfo(path).volume == "1"
    assert await _series_row(maker, series.id) is None
    assert series.id not in series_api._deletions


async def test_repair_requested_after_a_deferred_delete_does_not_touch_the_archive(repair_db):
    session, maker, series, path = repair_db
    await issues_api.scan_issues(session)
    [issue] = await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    session.add(Download(
        series_id=series.id, chapter_id=series.chapters[0].id, kind=DownloadKind.DIRECT,
        status=DownloadStatus.DOWNLOADING, source_name="fake", payload="c1", title="Tracked 1",
    ))
    await session.commit()
    before = path.read_bytes()
    lock = series_api.series_lock(series.id)
    await lock.acquire()  # a refresh holds the series through slow source fetches

    async with asyncio.timeout(10):
        try:
            await _delete(maker, series.id, False)
            async with maker() as check:
                [download] = (await check.scalars(select(Download))).all()
                assert (download.status, download.error) == (DownloadStatus.FAILED, REMOVED_BY_USER)
            while not getattr(lock, "_waiters", None):
                await asyncio.sleep(0)  # the deletion is queued on the lock

            async def repair():
                async with maker() as repair_session:
                    await issues_api.resolve_issue(issue.id, IssueResolveIn(revision=issue.revision), repair_session)

            repairing = asyncio.create_task(repair())
            await asyncio.sleep(0)
        finally:
            lock.release()
        with pytest.raises(HTTPException) as rejected:
            await repairing
        await _finish_deletions()

    assert rejected.value.status_code == 404
    assert path.read_bytes() == before
    assert await _series_row(maker, series.id) is None
    async with maker() as check:
        assert (await check.scalars(select(Chapter))).all() == []


async def test_deferred_delete_is_recovered_after_its_task_is_cancelled(repair_db):
    _, maker, series, path = repair_db
    series_id = series.id
    async with series_api.series_lock(series_id):
        await _delete(maker, series_id, False)
        task = series_api._deletions[series_id]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async with maker() as check:
        saved = await check.get(Series, series_id)
        assert saved is not None and saved.deletion_pending
        with pytest.raises(HTTPException) as hidden:
            await series_api.get_series(series_id, check)
        assert hidden.value.status_code == 404

    await series_api.resume_pending_deletions()
    await _finish_deletions()
    assert await _series_row(maker, series_id) is None
    assert path.exists()


async def test_pending_deletion_hides_wanted_chapters_and_rejects_grabs_and_retries(repair_db, monkeypatch):
    session, maker, series, _ = repair_db
    series_id, chapter_id = series.id, series.chapters[0].id
    series.chapters[0].downloaded = False
    active = Series(title="Active", chapters=[Chapter(number=1)])
    failed = Download(series_id=series_id, kind=DownloadKind.TORRENT, status=DownloadStatus.FAILED)
    session.add_all([active, failed])
    await session.commit()
    active_id, download_id = active.id, failed.id

    monkeypatch.setattr(queue.registry, "apply_settings", AsyncMock(return_value={
        **settings_service.DEFAULTS, "qbittorrent_enabled": "true",
    }))
    submit = AsyncMock()
    verify = AsyncMock()
    monkeypatch.setattr(queue, "enqueue_torrent", submit)
    monkeypatch.setattr(queue, "submit_torrent", submit)
    monkeypatch.setattr(queue, "_verify_direct_availability", verify)
    monkeypatch.setattr(tasks, "submit_torrent", submit)
    async with series_api.series_lock(series_id):
        await _delete(maker, series_id, False)
        async with maker() as check:
            wanted = await system.wanted_page(limit=100, offset=0, session=check)
            assert wanted["total"] == 1
            assert [item.series_id for item in wanted["items"]] == [active_id]
            for grab in (
                GrabIn(chapter_id=chapter_id, source_name="fake", external_id="c1"),
                GrabIn(series_id=series_id, magnet="magnet:?xt=urn:btih:" + "a" * 40),
            ):
                with pytest.raises(HTTPException) as rejected:
                    await queue.grab(grab, check)
                assert rejected.value.status_code == 404
            with pytest.raises(HTTPException) as rejected:
                await queue.retry_failed_download(download_id, check)
            assert rejected.value.status_code == 404
        with pytest.raises(tasks.DownloadCancelled):
            await tasks.enqueue_torrent(session, series, "unused", "release", settings_service.DEFAULTS)
    await _finish_deletions()
    submit.assert_not_awaited()
    verify.assert_not_awaited()
