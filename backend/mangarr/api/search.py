from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..metadata.anilist import provider as anilist
from ..metadata.mangaupdates import provider as mangaupdates
from ..models import Chapter, Series
from ..schemas import MetadataResult, ReleaseOut
from ..sources import registry

router = APIRouter(prefix="/search", tags=["search"])


@router.get("/metadata", response_model=list[MetadataResult])
async def search_metadata(
    q: str, provider: str = "mangaupdates", session: AsyncSession = Depends(get_session)
):
    meta_provider = anilist if provider == "anilist" else mangaupdates
    id_column = Series.anilist_id if provider == "anilist" else Series.mangaupdates_id
    results = await meta_provider.search(q)
    in_library = {
        row[0]
        for row in (await session.execute(select(id_column))).all()
        if row[0] is not None
    }
    return [
        MetadataResult(
            provider=r.provider,
            provider_id=r.provider_id,
            title=r.title,
            alt_titles=r.alt_titles,
            description=r.description,
            status=r.status,
            year=r.year,
            cover_url=r.cover_url,
            genres=r.genres,
            total_chapters=r.total_chapters,
            total_volumes=r.total_volumes,
            in_library=int(r.provider_id) in in_library,
        )
        for r in results
    ]


@router.get("/releases", response_model=list[ReleaseOut])
async def search_releases(
    series_id: int | None = None,
    chapter_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Interactive search. With chapter_id: direct-source releases for that
    chapter. With series_id only: torrent releases (+ nothing chapter-specific)."""
    chapter = None
    if chapter_id is not None:
        chapter = await session.get(Chapter, chapter_id)
        if chapter is None:
            raise HTTPException(404, "Chapter not found")
        series_id = chapter.series_id
    if series_id is None:
        raise HTTPException(422, "series_id or chapter_id required")

    result = await session.execute(
        select(Series).options(selectinload(Series.source_links)).where(Series.id == series_id)
    )
    series = result.scalar_one_or_none()
    if series is None:
        raise HTTPException(404, "Series not found")

    values = await registry.apply_settings(session)
    releases: list[ReleaseOut] = []
    links = {sl.source_name: sl for sl in series.source_links}

    if chapter is not None:
        for src in registry.enabled_direct_sources(values):
            link = links.get(src.name)
            if link is None:
                continue
            try:
                source_chapters = await src.list_chapters(link.external_id)
            except Exception:
                continue
            for sc in source_chapters:
                if sc.number == chapter.number:
                    releases.append(
                        ReleaseOut(
                            kind="direct",
                            source_name=src.name,
                            title=f"{series.title} - Chapter {sc.number:g}"
                                  + (f" - {sc.title}" if sc.title else ""),
                            chapter_number=sc.number,
                            external_id=sc.external_id,
                            url=sc.url,
                        )
                    )

    if values["qbittorrent_enabled"] == "true":
        for indexer in registry.enabled_torrent_indexers(values):
            try:
                torrents = await indexer.search(series.title)
            except Exception:
                continue
            for t in torrents[:25]:
                releases.append(
                    ReleaseOut(
                        kind="torrent",
                        source_name=indexer.name,
                        title=t.title,
                        magnet=t.magnet,
                        url=t.url,
                        size_bytes=t.size_bytes,
                        seeders=t.seeders,
                        leechers=t.leechers,
                    )
                )
    return releases
