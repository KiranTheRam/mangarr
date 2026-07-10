import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Integer, cast, func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..jobs.tasks import REFRESHING, refresh_series_full
from ..metadata.anilist import provider as anilist
from ..metadata.mangaupdates import provider as mangaupdates
from ..models import Chapter, Series, SeriesFolder, SeriesStatus
from ..schemas import (
    AddSeriesIn,
    ChapterMetadataIn,
    ChapterMonitorIn,
    ChapterOut,
    SeriesDetailOut,
    SeriesOut,
    SeriesUpdateIn,
)
from ..titles import english_title, split_alt_titles, unique_titles
from ..util import sanitize_filename

router = APIRouter(prefix="/series", tags=["series"])


def _series_out(series: Series, chapter_count: int, downloaded_count: int) -> SeriesOut:
    out = SeriesOut.model_validate(series)
    out.english_title = english_title(series.title, split_alt_titles(series.alt_titles))
    out.chapter_count = chapter_count
    out.downloaded_count = downloaded_count
    return out


async def _normalize_folder_name(session: AsyncSession, series: Series, folder_name: str) -> str:
    """Store the folder relative to the series' root folder when the given path
    is under it (so it survives a root-folder move); otherwise keep as given."""
    from pathlib import Path

    from ..models import RootFolder

    folder_name = folder_name.strip()
    if series.root_folder_id is not None and folder_name.startswith("/"):
        root = await session.get(RootFolder, series.root_folder_id)
        if root is not None:
            try:
                return str(Path(folder_name).relative_to(root.path))
            except ValueError:
                pass  # outside the root — keep absolute
    return folder_name.strip("/") if not folder_name.startswith("/") else folder_name


@router.get("", response_model=list[SeriesOut])
async def list_series(session: AsyncSession = Depends(get_session)):
    counts: dict[int, tuple[int, int]] = {}
    rows = await session.execute(
        select(
            Chapter.series_id,
            func.count(Chapter.id),
            func.sum(cast(Chapter.downloaded, Integer)),
        ).group_by(Chapter.series_id)
    )
    for series_id, total, downloaded in rows.all():
        counts[series_id] = (total, int(downloaded or 0))
    result = await session.execute(select(Series).order_by(Series.sort_title, Series.title))
    return [
        _series_out(s, *counts.get(s.id, (0, 0)))
        for s in result.scalars().all()
    ]


@router.post("", response_model=SeriesDetailOut, status_code=201)
async def add_series(body: AddSeriesIn, session: AsyncSession = Depends(get_session)):
    if (body.mangaupdates_id is None) == (body.anilist_id is None):
        raise HTTPException(422, "Provide exactly one of mangaupdates_id or anilist_id")
    if body.mangaupdates_id is not None:
        id_filter = Series.mangaupdates_id == body.mangaupdates_id
        provider, provider_id = mangaupdates, body.mangaupdates_id
    else:
        id_filter = Series.anilist_id == body.anilist_id
        provider, provider_id = anilist, body.anilist_id
    existing = await session.execute(select(Series).where(id_filter))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(409, "Series already in library")
    meta = await provider.get_series(str(provider_id))
    if meta is None:
        raise HTTPException(404, f"{provider.name} series not found")
    alt_titles = unique_titles([*meta.alt_titles, body.english_title, *body.alt_titles])
    series = Series(
        anilist_id=body.anilist_id,
        mangaupdates_id=body.mangaupdates_id,
        title=meta.title,
        sort_title=meta.title.lower(),
        alt_titles="\n".join(alt_titles),
        description=meta.description,
        status=SeriesStatus(meta.status),
        year=meta.year,
        cover_url=meta.cover_url,
        banner_url=meta.banner_url,
        genres=",".join(meta.genres),
        total_chapters=meta.total_chapters,
        total_volumes=meta.total_volumes,
        monitored=body.monitored,
        root_folder_id=body.root_folder_id,
        folder_name=sanitize_filename(meta.title),
        folder_pinned=body.folder_pinned,
    )
    if body.folder_name.strip():
        series.folder_name = await _normalize_folder_name(session, series, body.folder_name)
    session.add(series)
    for extra in body.extra_folders:
        path = await _normalize_folder_name(session, series, extra)
        if path and path != series.folder_name:
            series.extra_folders.append(SeriesFolder(path=path))
    await session.commit()
    await session.refresh(series)
    # link sources + fetch chapters in the background; "search now" adds queue
    # available missing chapters as soon as the library scan and metadata
    # refresh have run, instead of waiting for the next monitor interval.
    # Pre-mark so the series page the UI jumps to shows the work in progress
    # even before the task's first tick.
    REFRESHING.add(series.id)
    asyncio.get_running_loop().create_task(
        refresh_series_full(series.id, grab_missing=body.search_now)
    )
    return await get_series(series.id, session)


@router.get("/{series_id}", response_model=SeriesDetailOut)
async def get_series(series_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Series)
        .options(selectinload(Series.chapters), selectinload(Series.source_links))
        .where(Series.id == series_id)
    )
    series = result.scalar_one_or_none()
    if series is None:
        raise HTTPException(404, "Series not found")
    out = SeriesDetailOut.model_validate(series)
    out.english_title = english_title(series.title, split_alt_titles(series.alt_titles))
    out.chapter_count = len(series.chapters)
    out.downloaded_count = sum(1 for c in series.chapters if c.downloaded)
    out.refreshing = series_id in REFRESHING
    return out


@router.put("/{series_id}", response_model=SeriesDetailOut)
async def update_series(
    series_id: int, body: SeriesUpdateIn, session: AsyncSession = Depends(get_session)
):
    series = await session.get(Series, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    if body.monitored is not None and body.monitored != series.monitored:
        series.monitored = body.monitored
        # monitoring a series means wanting all of its missing content, so the
        # chapter flags follow the toggle (the next monitor pass grabs every
        # missing chapter, not just ones added while monitored); per-chapter
        # toggles can then re-exclude individual chapters
        await session.execute(
            sa_update(Chapter)
            .where(Chapter.series_id == series_id)
            .values(monitored=body.monitored)
        )
    if body.root_folder_id is not None:
        series.root_folder_id = body.root_folder_id
    if body.folder_name is not None:
        series.folder_name = await _normalize_folder_name(session, series, body.folder_name)
        # an explicit folder edit is an explicit choice — pin it so the next
        # scan can't re-adopt a title-matching folder over it
        if body.folder_pinned is None:
            series.folder_pinned = True
    if body.folder_pinned is not None:
        series.folder_pinned = body.folder_pinned
    await session.commit()
    return await get_series(series_id, session)


@router.delete("/{series_id}", status_code=204)
async def delete_series(series_id: int, session: AsyncSession = Depends(get_session)):
    series = await session.get(Series, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    await session.delete(series)
    await session.commit()


@router.post("/{series_id}/refresh", status_code=202)
async def refresh_series(
    series_id: int, wait: bool = False, session: AsyncSession = Depends(get_session)
):
    series = await session.get(Series, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    if wait:
        # synchronous variant for flows that need the refreshed state next
        # (e.g. the volume-resync preview right after a refresh)
        await refresh_series_full(series_id)
        return {"status": "refreshed"}
    REFRESHING.add(series_id)
    asyncio.get_running_loop().create_task(refresh_series_full(series_id))
    return {"status": "refreshing"}


@router.put("/{series_id}/chapters/monitor", status_code=204)
async def monitor_chapters(
    series_id: int, body: ChapterMonitorIn, session: AsyncSession = Depends(get_session)
):
    result = await session.execute(
        select(Chapter).where(Chapter.series_id == series_id, Chapter.id.in_(body.chapter_ids))
    )
    for chapter in result.scalars().all():
        chapter.monitored = body.monitored
    await session.commit()


@router.put("/{series_id}/chapters/{chapter_id}/metadata", response_model=ChapterOut)
async def update_chapter_metadata(
    series_id: int,
    chapter_id: int,
    body: ChapterMetadataIn,
    session: AsyncSession = Depends(get_session),
):
    chapter = await session.get(Chapter, chapter_id)
    if chapter is None or chapter.series_id != series_id:
        raise HTTPException(404, "Chapter not found")
    # only an actual edit becomes "manual" provenance; saving an unchanged
    # value (e.g. locking a wikipedia title in place) keeps its real source
    title = body.title.strip()
    if title != chapter.title:
        chapter.title = title
        chapter.title_source = "manual" if title else ""
    if body.volume != chapter.volume:
        chapter.volume = body.volume
        chapter.volume_source = "manual" if body.volume is not None else ""
    chapter.title_locked = body.title_locked
    chapter.volume_locked = body.volume_locked
    await session.commit()
    await session.refresh(chapter)
    return chapter
