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
from ..sources.base import DirectSource
from ..titles import english_title, split_alt_titles, title_queries
from ..util import normalize_title

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
            english_title=english_title(r.title, r.alt_titles, q),
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


def _candidate_matches_series(candidate_titles: list[str], wanted: set[str]) -> bool:
    if not wanted:
        return False
    cand_titles = {n for title in candidate_titles if (n := normalize_title(title))}
    return bool(wanted & cand_titles)


async def _find_direct_source_ids(
    src: DirectSource,
    queries: list[str],
) -> list[tuple[str, str, str]]:
    wanted = {n for title in queries if (n := normalize_title(title))}
    matches: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for query in queries:
        if not query.strip():
            continue
        try:
            candidates = await src.search_series(query)
        except Exception:
            return matches
        nq = normalize_title(query)
        for cand in candidates:
            if cand.external_id in seen:
                continue
            cand_titles = [cand.title, *cand.alt_titles]
            if _candidate_matches_series(cand_titles, wanted):
                matches.append((cand.external_id, cand.title, cand.url))
                seen.add(cand.external_id)
        if matches:
            continue
        if nq and len(nq) >= 4 and candidates:
            top = candidates[0]
            if top.external_id not in seen and normalize_title(top.title).startswith(nq[:12]):
                matches.append((top.external_id, top.title, top.url))
                seen.add(top.external_id)
    return matches


@router.get("/releases", response_model=list[ReleaseOut])
async def search_releases(
    series_id: int | None = None,
    chapter_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Interactive search across direct sources and torrent indexers.

    With chapter_id, direct results are restricted to that chapter. With
    series_id only, direct results are missing local chapters that sources can
    serve, plus torrent releases for the series.
    """
    chapter = None
    if chapter_id is not None:
        chapter = await session.get(Chapter, chapter_id)
        if chapter is None:
            raise HTTPException(404, "Chapter not found")
        series_id = chapter.series_id
    if series_id is None:
        raise HTTPException(422, "series_id or chapter_id required")

    result = await session.execute(
        select(Series)
        .options(selectinload(Series.source_links), selectinload(Series.chapters))
        .where(Series.id == series_id)
    )
    series = result.scalar_one_or_none()
    if series is None:
        raise HTTPException(404, "Series not found")

    values = await registry.apply_settings(session)
    releases: list[ReleaseOut] = []
    links = {sl.source_name: sl for sl in series.source_links}
    alt_titles = split_alt_titles(series.alt_titles)
    queries = title_queries(series.title, alt_titles)
    local_chapters = {ch.number: ch for ch in series.chapters}

    direct_seen: set[tuple[str, str]] = set()
    for src in registry.enabled_direct_sources(values):
        link = links.get(src.name)
        source_ids = (
            [(link.external_id, link.external_title, link.external_url)]
            if link is not None
            else await _find_direct_source_ids(src, queries)
        )
        for external_id, _title, _url in source_ids:
            try:
                source_chapters = await src.list_chapters(external_id)
            except Exception:
                continue
            added_for_source = 0
            for sc in source_chapters:
                if (src.name, sc.external_id) in direct_seen:
                    continue
                local_chapter = chapter if chapter is not None else local_chapters.get(sc.number)
                if local_chapter is None:
                    continue
                if chapter is not None and sc.number != chapter.number:
                    continue
                if chapter is None and local_chapter.downloaded:
                    continue
                direct_seen.add((src.name, sc.external_id))
                releases.append(
                    ReleaseOut(
                        kind="direct",
                        source_name=src.name,
                        title=f"{series.title} - Chapter {sc.number:g}"
                              + (f" - {sc.title}" if sc.title else ""),
                        chapter_id=local_chapter.id,
                        chapter_number=sc.number,
                        external_id=sc.external_id,
                        url=sc.url,
                    )
                )
                added_for_source += 1
                if chapter is None and added_for_source >= 60:
                    break

    if values["qbittorrent_enabled"] == "true":
        for indexer in registry.enabled_torrent_indexers(values):
            torrent_seen: set[str] = set()
            for query in queries:
                try:
                    torrents = await indexer.search(query)
                except Exception:
                    continue
                for t in torrents[:25]:
                    key = t.magnet or t.url or t.title
                    if key in torrent_seen:
                        continue
                    torrent_seen.add(key)
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
