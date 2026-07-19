"""Retry behavior for failed Activity downloads."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.api import queue
from mangarr.models import (
    Base,
    Chapter,
    Download,
    DownloadKind,
    DownloadStatus,
    HistoryEvent,
    Series,
)


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _failed_direct(session):
    series = Series(title="Test Series", sort_title="test series")
    chapter = Chapter(number=1.0, monitored=True)
    series.chapters.append(chapter)
    session.add(series)
    await session.flush()
    dl = Download(
        series_id=series.id,
        chapter_id=chapter.id,
        kind=DownloadKind.DIRECT,
        status=DownloadStatus.FAILED,
        title="Test Series - Chapter 1",
        source_name="fake",
        payload="chapter-1",
        progress=0.4,
        error="temporary failure",
    )
    session.add(dl)
    await session.commit()
    return dl


async def test_retry_failed_direct_download_requeues_same_item(db_session):
    dl = await _failed_direct(db_session)

    out = await queue.retry_failed_download(dl.id, db_session)

    await db_session.refresh(dl)
    assert out.id == dl.id
    assert out.status == DownloadStatus.QUEUED
    assert out.series_title == "Test Series"
    assert dl.progress == 0.0
    assert dl.error == ""
    event = (await db_session.execute(select(HistoryEvent))).scalar_one()
    assert event.event == "retried"
    assert event.chapter_id == dl.chapter_id


async def test_retry_rejects_non_failed_download(db_session):
    dl = await _failed_direct(db_session)
    dl.status = DownloadStatus.QUEUED
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await queue.retry_failed_download(dl.id, db_session)

    assert exc.value.status_code == 409


async def test_retry_failed_torrent_resubmits_stored_magnet(db_session, monkeypatch):
    magnet = "magnet:?xt=urn:btih:" + "a" * 40
    dl = Download(
        kind=DownloadKind.TORRENT,
        status=DownloadStatus.FAILED,
        title="Test pack",
        source_name="nyaa",
        payload=magnet,
        error="torrent disappeared",
    )
    db_session.add(dl)
    await db_session.commit()
    submitted = []

    async def fake_settings(session):
        return {"qbittorrent_enabled": "true"}

    async def fake_submit(payload, values):
        submitted.append(payload)
        return "a" * 40

    monkeypatch.setattr(queue.registry, "apply_settings", fake_settings)
    monkeypatch.setattr(queue, "submit_torrent", fake_submit)

    out = await queue.retry_failed_download(dl.id, db_session)

    assert submitted == [magnet]
    assert out.status == DownloadStatus.DOWNLOADING
    assert dl.torrent_hash == "a" * 40
    assert dl.error == ""
