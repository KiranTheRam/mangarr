import asyncio
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Integer, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .. import settings_service
from ..adding import (
    AddOptions,
    LibraryIndex,
    MetadataNotFound,
    SeriesExists,
    create_series,
    normalize_folder_name,
    start_refreshes,
)
from ..automation import RESOLVED_MODES, apply_monitor_mode, order_sources
from ..db import get_session
from ..jobs.tasks import (
    REFRESHING,
    _notify_kavita,
    acquire_series_lock,
    merge_complete_volumes,
    refresh_series_full,
    try_acquire_series_lock,
)
from ..library.move import MoveError, anchor_extra_folders, move_series_folder
from ..models import Chapter, Download, DownloadStatus, RootFolder, Series
from ..release_schedule import cadence_label, release_schedule
from ..schemas import (
    AddSeriesIn,
    ChapterMetadataIn,
    ChapterMonitorIn,
    ChapterOut,
    RelatedOut,
    RelatedTitleOut,
    SeriesBulkIn,
    SeriesBulkOut,
    SeriesBulkRefreshIn,
    SeriesDetailOut,
    SeriesEditorIn,
    SeriesEditorOut,
    SeriesEditorProblemOut,
    SeriesGroupOut,
    SeriesOut,
    SeriesUpdateIn,
    VolumeMergeOut,
)
from ..sources import registry
from ..titles import english_title, split_alt_titles
from ..util import is_special_chapter

router = APIRouter(prefix="/series", tags=["series"])


class _Counts(NamedTuple):
    """Chapter tallies split so specials never count toward completion."""
    chapters: int = 0
    downloaded: int = 0
    specials: int = 0
    specials_downloaded: int = 0


def _count_chapters(chapters: Iterable[Chapter]) -> _Counts:
    totals = [0, 0, 0, 0]
    for c in chapters:
        if c.excluded:
            continue
        offset = 2 if is_special_chapter(c.number) else 0
        totals[offset] += 1
        if c.downloaded:
            totals[offset + 1] += 1
    return _Counts(*totals)


def _apply_counts(out: SeriesOut, counts: _Counts) -> None:
    out.chapter_count = counts.chapters
    out.downloaded_count = counts.downloaded
    out.special_count = counts.specials
    out.special_downloaded_count = counts.specials_downloaded


def _series_out(series: Series, counts: _Counts) -> SeriesOut:
    out = SeriesOut.model_validate(series)
    out.english_title = english_title(series.title, split_alt_titles(series.alt_titles))
    _apply_counts(out, counts)
    return out


def _content_source_names(values: dict[str, str]) -> list[str]:
    """Enabled sources that can serve chapters, in global priority order
    (metadata-only sources such as VIZ are never grabbed from)."""
    return [
        src.name for src in registry.enabled_direct_sources(values)
        if src.name in settings_service.CONTENT_SOURCE_NAMES
    ]


def _check_source_names(names: list[str], field: str) -> None:
    unknown = [n for n in names if n not in settings_service.CONTENT_SOURCE_NAMES]
    if unknown:
        raise HTTPException(422, f"{field}: unknown source(s) {', '.join(unknown)}")


def _apply_monitoring(
    series: Series,
    monitored: bool | None,
    monitor_mode: str | None,
    monitor_from: float | None,
) -> None:
    """Apply a monitored toggle and/or monitoring mode, re-deriving every
    chapter's monitored flag when either changes. `series.chapters` must be
    loaded."""
    reapply_monitoring = False
    if monitored is not None and monitored != series.monitored:
        series.monitored = monitored
        # monitoring a series means wanting what its mode covers, so the
        # chapter flags follow the toggle (the next monitor pass grabs every
        # chapter the mode wants, not just ones added while monitored);
        # per-chapter toggles can then re-exclude individual chapters
        reapply_monitoring = True
    if monitor_mode is not None:
        if monitor_mode == "from_chapter":
            threshold = monitor_from if monitor_from is not None else series.monitor_from
            if threshold is None:
                raise HTTPException(422, "monitor_from is required for monitor_mode from_chapter")
            series.monitor_from = threshold
        elif monitor_mode not in RESOLVED_MODES:
            series.monitor_from = None
        # a derived threshold ("future", "latest volume") is re-resolved
        # against today's chapter list by apply_monitor_mode below
        series.monitor_mode = monitor_mode
        reapply_monitoring = True
    elif (
        monitor_from is not None
        and series.monitor_mode == "from_chapter"
        and monitor_from != series.monitor_from
    ):
        series.monitor_from = monitor_from
        reapply_monitoring = True
    if reapply_monitoring:
        apply_monitor_mode(series, series.chapters, resolve=monitor_mode is not None)


def _clean_lines(names: list[str]) -> str:
    """Join group names one per line, dropping blanks and repeats."""
    out: list[str] = []
    for name in names:
        name = " ".join(name.split())
        if name and name not in out:
            out.append(name)
    return "\n".join(out)


@router.get("", response_model=list[SeriesOut])
async def list_series(session: AsyncSession = Depends(get_session)):
    # the SQL mirror of util.is_special_chapter: a cast to INTEGER truncates the
    # fractional part, so a decimal chapter no longer equals its own number
    special = cast(Chapter.number, Integer) != Chapter.number
    downloaded = cast(Chapter.downloaded, Integer)
    counts: dict[int, _Counts] = {}
    rows = await session.execute(
        select(
            Chapter.series_id,
            func.sum(case((special, 0), else_=1)),
            func.sum(case((special, 0), else_=downloaded)),
            func.sum(case((special, 1), else_=0)),
            func.sum(case((special, downloaded), else_=0)),
        )
        .where(Chapter.excluded == False)  # noqa: E712
        .group_by(Chapter.series_id)
    )
    for series_id, *totals in rows.all():
        counts[series_id] = _Counts(*(int(t or 0) for t in totals))
    result = await session.execute(select(Series).order_by(Series.sort_title, Series.title))
    return [
        _series_out(s, counts.get(s.id, _Counts()))
        for s in result.scalars().all()
    ]


@router.post("", response_model=SeriesDetailOut, status_code=201)
async def add_series(body: AddSeriesIn, session: AsyncSession = Depends(get_session)):
    if (body.mangaupdates_id is None) == (body.anilist_id is None):
        raise HTTPException(422, "Provide exactly one of mangaupdates_id or anilist_id")
    if body.monitor_mode == "from_chapter" and body.monitor_from is None:
        raise HTTPException(422, "monitor_from is required for monitor_mode from_chapter")
    opts = AddOptions(
        root_folder_id=body.root_folder_id,
        monitored=body.monitored,
        monitor_mode=body.monitor_mode,
        monitor_from=body.monitor_from,
        english_title=body.english_title,
        alt_titles=body.alt_titles,
        folder_name=body.folder_name,
        folder_pinned=body.folder_pinned,
        extra_folders=body.extra_folders,
    )
    try:
        series = await create_series(
            session, opts, anilist_id=body.anilist_id, mangaupdates_id=body.mangaupdates_id
        )
    except SeriesExists:
        raise HTTPException(409, "Series already in library") from None
    except MetadataNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    # link sources + fetch chapters in the background; "search now" adds queue
    # available missing chapters as soon as the library scan and metadata
    # refresh have run, instead of waiting for the next monitor interval.
    start_refreshes([series.id], grab_missing=body.search_now)
    return await get_series(series.id, session)


# ---------------------------------------------------------- library editor
# (declared before the /{series_id} routes, which would otherwise claim
# "editor" as a series id)

async def _series_in_order(session: AsyncSession, series_ids: list[int], *options) -> list[Series]:
    """The requested series that exist, in the order they were given."""
    result = await session.execute(select(Series).options(*options).where(Series.id.in_(series_ids)))
    by_id = {s.id: s for s in result.scalars().all()}
    return [by_id[i] for i in dict.fromkeys(series_ids) if i in by_id]


async def _change_root_folder(
    session: AsyncSession, series: Series, new_root: RootFolder, move_files: bool
) -> bool:
    """Point the series at another root folder, optionally moving its folder
    there first. Returns whether files moved; raises MoveError (with nothing
    changed) when the move can't happen."""
    if not move_files or series.root_folder is None:
        if series.root_folder is not None:
            anchor_extra_folders(series, Path(series.root_folder.path))
        series.root_folder = new_root
        return False
    in_flight = await session.scalar(
        select(Download.id).where(
            Download.series_id == series.id,
            Download.status.in_([
                DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING, DownloadStatus.IMPORTING,
            ]),
        ).limit(1)
    )
    if in_flight is not None:
        # an import would write into the old folder after it moved away
        raise MoveError("downloads are in progress; move it once they finish")
    lock = await try_acquire_series_lock(series.id)
    if lock is None:
        raise MoveError("the series is refreshing; try again once it finishes")
    try:
        old_root = Path(series.root_folder.path)
        moved = await move_series_folder(series, new_root)
        anchor_extra_folders(series, old_root)
        series.root_folder = new_root
        # commit while still holding the lock, so the next refresh already
        # sees the files at their new paths
        await session.commit()
    finally:
        lock.release()
    return moved


@router.put("/editor", response_model=SeriesEditorOut)
async def edit_series_bulk(body: SeriesEditorIn, session: AsyncSession = Depends(get_session)):
    """Change monitoring and/or the root folder of many series at once.
    A series that can't be moved is reported and left in its old root folder;
    the rest of the change still applies to it."""
    if body.monitor_mode == "from_chapter" and body.monitor_from is None:
        raise HTTPException(422, "monitor_from is required for monitor_mode from_chapter")
    new_root = None
    if body.root_folder_id is not None:
        new_root = await session.get(RootFolder, body.root_folder_id)
        if new_root is None:
            raise HTTPException(404, "Root folder not found")
    series_list = await _series_in_order(
        session, body.series_ids,
        selectinload(Series.chapters), selectinload(Series.extra_folders),
        selectinload(Series.root_folder),
    )
    values = await settings_service.get_all(session) if new_root and body.move_files else {}
    out = SeriesEditorOut(updated=0)
    for series in series_list:
        _apply_monitoring(series, body.monitored, body.monitor_mode, body.monitor_from)
        if new_root is not None and series.root_folder_id != new_root.id:
            try:
                moved = await _change_root_folder(session, series, new_root, body.move_files)
            except MoveError as exc:
                out.problems.append(
                    SeriesEditorProblemOut(series_id=series.id, title=series.title, detail=str(exc))
                )
                continue
            if moved:
                out.moved += 1
                _notify_kavita(values, series)
        out.updated += 1
    await session.commit()
    return out


@router.post("/editor/refresh", response_model=SeriesBulkOut, status_code=202)
async def refresh_series_bulk(
    body: SeriesBulkRefreshIn, session: AsyncSession = Depends(get_session)
):
    """Refresh many series in the background, one at a time; with
    search_missing, also queue the missing chapters their monitoring wants
    (an on-demand monitor pass)."""
    series_ids = [s.id for s in await _series_in_order(session, body.series_ids)]
    start_refreshes(series_ids, grab_missing=body.search_missing, only_monitored=True)
    return SeriesBulkOut(count=len(series_ids))


@router.post("/editor/delete", response_model=SeriesBulkOut)
async def delete_series_bulk(body: SeriesBulkIn, session: AsyncSession = Depends(get_session)):
    """Remove many series from the library. Files on disk are kept."""
    series_list = await _series_in_order(session, body.series_ids)
    for series in series_list:
        await session.delete(series)
    await session.commit()
    return SeriesBulkOut(count=len(series_list))


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
    _apply_counts(out, _count_chapters(series.chapters))
    out.refreshing = series_id in REFRESHING
    out.global_source_order = _content_source_names(await settings_service.get_all(session))
    out.effective_source_order = order_sources(out.global_source_order, series)
    schedule = release_schedule(series.chapters, series.status)
    out.cadence_days = schedule.cadence_days
    out.cadence_label = cadence_label(schedule.cadence_days)
    out.last_released_at = schedule.last_released_at
    out.next_expected_at = schedule.next_expected_at
    return out


@router.put("/{series_id}", response_model=SeriesDetailOut)
async def update_series(
    series_id: int, body: SeriesUpdateIn, session: AsyncSession = Depends(get_session)
):
    result = await session.execute(
        select(Series).options(selectinload(Series.chapters)).where(Series.id == series_id)
    )
    series = result.scalar_one_or_none()
    if series is None:
        raise HTTPException(404, "Series not found")
    _apply_monitoring(series, body.monitored, body.monitor_mode, body.monitor_from)
    if body.source_priority is not None:
        _check_source_names(body.source_priority, "source_priority")
        series.source_priority = ",".join(dict.fromkeys(body.source_priority))
    if body.blocked_sources is not None:
        _check_source_names(body.blocked_sources, "blocked_sources")
        series.blocked_sources = ",".join(dict.fromkeys(body.blocked_sources))
    if body.preferred_groups is not None:
        series.preferred_groups = _clean_lines(body.preferred_groups)
    if body.blocked_groups is not None:
        series.blocked_groups = _clean_lines(body.blocked_groups)
    if body.upgrades_enabled is not None:
        series.upgrades_enabled = body.upgrades_enabled
    if body.upgrade_cutoff is not None:
        if body.upgrade_cutoff:
            _check_source_names([body.upgrade_cutoff], "upgrade_cutoff")
        series.upgrade_cutoff = body.upgrade_cutoff
    if body.merge_volumes is not None:
        series.merge_volumes = body.merge_volumes
    if body.root_folder_id is not None:
        series.root_folder_id = body.root_folder_id
    if body.folder_name is not None:
        series.folder_name = await normalize_folder_name(session, series, body.folder_name)
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
    if body.excluded is not None:
        chapter.excluded = body.excluded
    await session.commit()
    await session.refresh(chapter)
    return chapter


@router.get("/{series_id}/groups", response_model=list[SeriesGroupOut])
async def list_series_groups(series_id: int, session: AsyncSession = Depends(get_session)):
    """Scanlation groups the series' linked sources offer, with how many
    chapters each covers — the choices for preferred/blocked groups. Asks
    the sources live, so it is only fetched on demand."""
    result = await session.execute(
        select(Series).options(selectinload(Series.source_links)).where(Series.id == series_id)
    )
    series = result.scalar_one_or_none()
    if series is None:
        raise HTTPException(404, "Series not found")
    values = await registry.apply_settings(session)
    links = {sl.source_name: sl for sl in series.source_links}
    sources = [src for src in registry.enabled_direct_sources(values) if src.name in links]

    async def groups_of(src) -> list[SeriesGroupOut]:
        chapters = await src.list_chapters(links[src.name].external_id)
        numbers: dict[str, set[float]] = {}
        for sc in chapters:
            if sc.group:
                numbers.setdefault(sc.group, set()).add(sc.number)
        return [
            SeriesGroupOut(source_name=src.name, group=group, chapters=len(covered))
            for group, covered in sorted(numbers.items(), key=lambda kv: -len(kv[1]))
        ]

    listings = await asyncio.gather(*(groups_of(src) for src in sources), return_exceptions=True)
    out: list[SeriesGroupOut] = []
    for listing in listings:
        if not isinstance(listing, BaseException):
            out.extend(listing)
    return out


@router.post("/{series_id}/volumes/merge", response_model=VolumeMergeOut)
async def merge_volumes_now(series_id: int, session: AsyncSession = Depends(get_session)):
    """Pack every closed, fully downloaded volume into one archive now,
    whether or not the series merges automatically."""
    from ..jobs.tasks import _load_series

    lock = await acquire_series_lock(series_id)
    try:
        series = await _load_series(session, series_id)
        if series is None:
            raise HTTPException(404, "Series not found")
        values = await settings_service.get_all(session)
        merged = await merge_complete_volumes(session, series, values, force=True)
    finally:
        lock.release()
    return VolumeMergeOut(merged=merged)


@router.get("/{series_id}/related", response_model=RelatedOut)
async def series_related(series_id: int, session: AsyncSession = Depends(get_session)):
    """Sequels, side stories and similar manga, each marked with the library
    series it already is (by provider id or any shared title)."""
    from ..related import related_titles

    series = await session.get(Series, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    try:
        related = await related_titles(series)
    except Exception as exc:
        raise HTTPException(502, f"Metadata providers unavailable: {exc}") from exc

    library = await LibraryIndex.load(session)

    def out(item) -> RelatedTitleOut:
        pid = int(item.provider_id)
        return RelatedTitleOut(
            provider=item.provider,
            provider_id=item.provider_id,
            title=item.title,
            alt_titles=item.alt_titles,
            cover_url=item.cover_url,
            year=item.year,
            status=item.status,
            format=item.format,
            relation=item.relation,
            in_library_series_id=library.find(
                pid if item.provider == "anilist" else None,
                pid if item.provider == "mangaupdates" else None,
                [item.title, *item.alt_titles],
            ),
        )

    return RelatedOut(
        relations=[out(r) for r in related.relations],
        recommendations=[out(r) for r in related.recommendations],
    )
