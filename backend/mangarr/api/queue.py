import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..download.qbittorrent import QbtClient
from ..jobs.tasks import REMOVED_BY_USER, enqueue_direct, enqueue_torrent, magnet_btih_hex
from ..models import (
    Chapter,
    Download,
    DownloadKind,
    DownloadStatus,
    HistoryEvent,
    Series,
)
from ..schemas import GrabIn, HistoryOut, QueueItemOut, QueueRemoveIn, QueueRemoveOut
from ..sources import registry

log = logging.getLogger(__name__)

router = APIRouter(tags=["queue"])

ACTIVE = [DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING, DownloadStatus.IMPORTING]

# failed downloads stay visible in the queue this long — otherwise a failure
# silently disappears from Activity and looks like the grab never happened
FAILED_VISIBLE_FOR = timedelta(hours=48)


@router.get("/queue", response_model=list[QueueItemOut])
async def get_queue(session: AsyncSession = Depends(get_session)):
    failed_cutoff = datetime.now(timezone.utc) - FAILED_VISIBLE_FOR
    result = await session.execute(
        select(Download, Series.title)
        .outerjoin(Series, Download.series_id == Series.id)
        .where(or_(
            Download.status.in_(ACTIVE),
            # user-removed items also carry FAILED, but hiding them is the
            # entire point of removal — only real failures stay visible
            and_(
                Download.status == DownloadStatus.FAILED,
                Download.error != REMOVED_BY_USER,
                Download.updated_at >= failed_cutoff,
            ),
        ))
        .order_by(Download.id)
    )
    items = []
    for dl, series_title in result.all():
        out = QueueItemOut.model_validate(dl)
        out.series_title = series_title or ""
        items.append(out)
    return items


async def _remove_downloads(session: AsyncSession, ids: list[int]) -> int:
    """Mark downloads as removed and stop their torrents in qBittorrent.

    Direct downloads that are merely queued never start (the queue worker only
    picks up QUEUED items); active torrents are also deleted from qBittorrent
    along with their partial data so 'remove' really stops the transfer.

    The REMOVED_BY_USER error text is significant: the monitor's retry logic
    ignores it, so cancelling a queued grab doesn't blacklist that source for
    the chapter the way a real download failure does. Dismissing an
    already-failed row works the same way — the failure is cleared from the
    queue view and the chapter becomes eligible for retry again."""
    result = await session.execute(
        select(Download).where(
            Download.id.in_(ids),
            Download.status.in_([*ACTIVE, DownloadStatus.FAILED]),
        )
    )
    downloads = result.scalars().all()
    hashes = [
        dl.torrent_hash for dl in downloads
        if dl.kind == DownloadKind.TORRENT and dl.torrent_hash
    ]
    for dl in downloads:
        dl.status = DownloadStatus.FAILED
        dl.error = REMOVED_BY_USER
    await session.commit()
    if hashes:
        values = await registry.apply_settings(session)
        if values["qbittorrent_enabled"] == "true":
            client = QbtClient(
                values["qbittorrent_url"], values["qbittorrent_username"],
                values["qbittorrent_password"],
            )
            try:
                await client.delete_torrents(hashes)
            except Exception as exc:
                log.warning("failed to delete %d torrent(s) from qBittorrent: %s",
                            len(hashes), exc)
            finally:
                await client.close()
    return len(downloads)


@router.delete("/queue/{download_id}", status_code=204)
async def remove_from_queue(download_id: int, session: AsyncSession = Depends(get_session)):
    removed = await _remove_downloads(session, [download_id])
    if removed == 0:
        raise HTTPException(404, "Download not found")


@router.post("/queue/remove", response_model=QueueRemoveOut)
async def remove_many_from_queue(
    body: QueueRemoveIn, session: AsyncSession = Depends(get_session)
):
    """Bulk removal for the queue's multi-select — one call stops and removes
    every selected download."""
    if not body.ids:
        raise HTTPException(422, "No download ids given")
    return QueueRemoveOut(removed=await _remove_downloads(session, body.ids))


@router.post("/queue/grab", response_model=QueueItemOut, status_code=201)
async def grab(body: GrabIn, session: AsyncSession = Depends(get_session)):
    values = await registry.apply_settings(session)

    if body.chapter_id is not None and body.source_name and body.external_id:
        chapter = await session.get(Chapter, body.chapter_id)
        if chapter is None:
            raise HTTPException(404, "Chapter not found")
        series = await session.get(Series, chapter.series_id)
        dl = await enqueue_direct(
            session, series, chapter, body.source_name, body.external_id
        )
    elif body.magnet:
        if values["qbittorrent_enabled"] != "true":
            raise HTTPException(400, "qBittorrent is not enabled in settings")
        if not magnet_btih_hex(body.magnet):
            raise HTTPException(422, "Magnet link must include a valid btih info hash")
        series = await session.get(Series, body.series_id) if body.series_id else None
        try:
            dl = await enqueue_torrent(
                session, series, body.magnet, body.title or "manual torrent", values
            )
        except Exception as exc:
            raise HTTPException(502, f"qBittorrent error: {exc}") from exc
    else:
        raise HTTPException(422, "Provide chapter_id+source_name+external_id or magnet")

    out = QueueItemOut.model_validate(dl)
    out.series_title = series.title if series else ""
    return out


@router.get("/history", response_model=list[HistoryOut])
async def get_history(limit: int = 100, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(HistoryEvent, Series.title)
        .outerjoin(Series, HistoryEvent.series_id == Series.id)
        .order_by(HistoryEvent.id.desc())
        .limit(limit)
    )
    items = []
    for ev, series_title in result.all():
        out = HistoryOut.model_validate(ev)
        out.series_title = series_title or ""
        items.append(out)
    return items
