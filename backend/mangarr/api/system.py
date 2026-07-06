from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..db import get_session
from ..models import Chapter, Download, DownloadStatus, RootFolder, Series
from ..schemas import RootFolderIn, RootFolderOut, SystemStatus, WantedItemOut

router = APIRouter(tags=["system"])


@router.get("/system/status", response_model=SystemStatus)
async def system_status(session: AsyncSession = Depends(get_session)):
    series_count = (await session.execute(select(func.count(Series.id)))).scalar_one()
    chapter_count = (await session.execute(select(func.count(Chapter.id)))).scalar_one()
    downloaded = (
        await session.execute(select(func.sum(cast(Chapter.downloaded, Integer))))
    ).scalar_one() or 0
    queue_count = (
        await session.execute(
            select(func.count(Download.id)).where(
                Download.status.in_(
                    [DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING, DownloadStatus.IMPORTING]
                )
            )
        )
    ).scalar_one()
    return SystemStatus(
        version=__version__,
        series_count=series_count,
        chapter_count=chapter_count,
        downloaded_count=int(downloaded),
        queue_count=queue_count,
    )


@router.get("/wanted", response_model=list[WantedItemOut])
async def wanted(limit: int = 100, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Chapter, Series.title, Series.cover_url)
        .join(Series, Chapter.series_id == Series.id)
        .where(
            Chapter.monitored == True,  # noqa: E712
            Chapter.downloaded == False,  # noqa: E712
            Series.monitored == True,  # noqa: E712
        )
        .order_by(Series.title, Chapter.number)
        .limit(limit)
    )
    return [
        WantedItemOut(
            chapter_id=ch.id,
            series_id=ch.series_id,
            series_title=title,
            cover_url=cover,
            number=ch.number,
            volume=ch.volume,
            title=ch.title,
        )
        for ch, title, cover in result.all()
    ]


@router.get("/rootfolders", response_model=list[RootFolderOut])
async def list_root_folders(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(RootFolder).order_by(RootFolder.id))
    return result.scalars().all()


@router.post("/rootfolders", response_model=RootFolderOut, status_code=201)
async def add_root_folder(body: RootFolderIn, session: AsyncSession = Depends(get_session)):
    path = Path(body.path).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, f"Cannot create folder: {exc}") from exc
    folder = RootFolder(path=str(path))
    session.add(folder)
    await session.commit()
    await session.refresh(folder)
    return folder


@router.delete("/rootfolders/{folder_id}", status_code=204)
async def delete_root_folder(folder_id: int, session: AsyncSession = Depends(get_session)):
    folder = await session.get(RootFolder, folder_id)
    if folder is None:
        raise HTTPException(404, "Root folder not found")
    await session.delete(folder)
    await session.commit()
