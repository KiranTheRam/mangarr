"""Existing-library endpoints: scan/adopt, preview+apply rename, per-series
file listing with manual mapping, and a filesystem folder browser."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .. import settings_service
from ..db import get_session
from ..library.matcher import find_media_files, match_files
from ..library.rename import apply_renames, plan_renames
from ..library.scanner import (
    find_existing_folder,
    resolve_folders,
    scan_series,
    series_dir,
)
from ..models import RootFolder, Series, SeriesFolder, SeriesSourceLink
from ..schemas import (
    CleanupApplyIn,
    CleanupFileOut,
    CleanupGroupOut,
    CleanupPlanOut,
    CleanupResultOut,
    FileMapIn,
    FileMapRangeIn,
    FileMapRangeOut,
    FilesystemEntryOut,
    FilesystemListOut,
    FolderPreviewIn,
    FolderPreviewOut,
    RenameApplyIn,
    RenameItemOut,
    RenameOutcomeOut,
    ResyncOut,
    ScanResultOut,
    SeriesFileOut,
    SeriesFolderIn,
    SeriesFolderOut,
    SourceCandidateOut,
    SourceLinkIn,
    SourceLinkOut,
    VolumeCandidateOut,
    VolumeDiffRowOut,
    VolumeResyncIn,
    VolumeResyncOut,
    VolumeResyncPreviewOut,
)
from ..sources import registry

router = APIRouter(tags=["library"])


async def _load(session: AsyncSession, series_id: int) -> Series:
    result = await session.execute(
        select(Series)
        .options(
            selectinload(Series.chapters),
            selectinload(Series.source_links),
            selectinload(Series.root_folder),
            selectinload(Series.extra_folders),
        )
        .where(Series.id == series_id)
    )
    series = result.scalar_one_or_none()
    if series is None:
        raise HTTPException(404, "Series not found")
    return series


def _root_of(series: Series) -> Path:
    if series.root_folder is None:
        raise HTTPException(400, "Series has no root folder configured")
    return Path(series.root_folder.path)


def _folders_of(series: Series) -> list[Path]:
    """Primary folder plus any extra folders configured for the series."""
    return resolve_folders(_root_of(series), series, [f.path for f in series.extra_folders])


# ------------------------------------------------------------------ scan

@router.post("/series/{series_id}/scan", response_model=ScanResultOut)
async def scan(series_id: int, session: AsyncSession = Depends(get_session)):
    series = await _load(session, series_id)
    root = _root_of(series)
    folders = _folders_of(series)
    # adopt a matching folder if the primary one doesn't exist yet — unless
    # the user picked the folder deliberately
    if not folders[0].exists() and not series.extra_folders and not series.folder_pinned:
        found = find_existing_folder(root, series)
        if found:
            series.folder_name = found
            folders = _folders_of(series)
    result = scan_series(series, list(series.chapters), folders)
    await session.commit()
    return ScanResultOut(
        folder=", ".join(str(f) for f in folders),
        folder_exists=any(f.exists() for f in folders),
        matched_chapters=result.matched_chapters,
        volume_files=result.volume_files,
        cleared=result.cleared,
        unmatched=[m.path.name for m in result.unmatched],
    )


@router.post("/library/scan", status_code=202)
async def scan_all():
    from ..jobs.tasks import scan_all_series

    asyncio.get_running_loop().create_task(scan_all_series())
    return {"status": "scanning"}


# ----------------------------------------------------------------- rename

async def _plan(session: AsyncSession, series: Series):
    values = await settings_service.get_all(session)
    return plan_renames(
        series, list(series.chapters),
        values["naming_template"], values["naming_template_no_volume"],
    )


@router.get("/series/{series_id}/rename", response_model=list[RenameItemOut])
async def rename_preview(series_id: int, session: AsyncSession = Depends(get_session)):
    series = await _load(session, series_id)
    return [
        RenameItemOut(
            chapter_ids=i.chapter_ids, current_path=i.current_path,
            current_name=i.current_name, new_path=i.new_path, new_name=i.new_name,
            conflict=i.conflict,
        )
        for i in await _plan(session, series)
    ]


@router.post("/series/{series_id}/rename", response_model=list[RenameOutcomeOut])
async def rename_apply(
    series_id: int, body: RenameApplyIn, session: AsyncSession = Depends(get_session)
):
    series = await _load(session, series_id)
    items = await _plan(session, series)
    if body.chapter_ids is not None:
        wanted = set(body.chapter_ids)
        items = [i for i in items if wanted & set(i.chapter_ids)]
    outcomes = apply_renames(items, {c.id: c for c in series.chapters})
    await session.commit()
    return [
        RenameOutcomeOut(
            current_name=o.item.current_name, new_name=o.item.new_name,
            status=o.status, detail=o.detail,
        )
        for o in outcomes
    ]


# --------------------------------------------------------- files + mapping

@router.get("/series/{series_id}/files", response_model=list[SeriesFileOut])
async def series_files(series_id: int, session: AsyncSession = Depends(get_session)):
    series = await _load(session, series_id)
    media = []
    for folder in _folders_of(series):
        if folder.exists():
            media.extend(find_media_files(folder))
    result = match_files(media, list(series.chapters))
    out: list[SeriesFileOut] = []
    for mf in result.matched:
        out.append(SeriesFileOut(
            path=str(mf.media.path), name=mf.media.path.name, is_dir=mf.media.is_dir,
            chapter_number=mf.media.chapter_number, volume_number=mf.media.volume_number,
            matched_chapter_id=mf.chapter.id if mf.chapter else None,
            covered_count=len(mf.covered_chapters),
        ))
    for m in result.unmatched:
        out.append(SeriesFileOut(
            path=str(m.path), name=m.path.name, is_dir=m.is_dir,
            chapter_number=m.chapter_number, volume_number=m.volume_number,
            matched_chapter_id=None,
        ))
    return out


@router.post("/series/{series_id}/files/map", status_code=204)
async def map_file(
    series_id: int, body: FileMapIn, session: AsyncSession = Depends(get_session)
):
    series = await _load(session, series_id)
    chapter = next((c for c in series.chapters if c.id == body.chapter_id), None)
    if chapter is None:
        raise HTTPException(404, "Chapter not found")
    if not Path(body.file_path).exists():
        raise HTTPException(400, "File not found on disk")
    chapter.downloaded = True
    chapter.file_path = body.file_path
    await session.commit()


@router.get("/series/{series_id}/cleanup", response_model=CleanupPlanOut)
async def cleanup_plan(series_id: int, session: AsyncSession = Depends(get_session)):
    from ..library.cleanup import analyze

    series = await _load(session, series_id)
    values = await settings_service.get_all(session)
    plan = analyze(series, list(series.chapters), _folders_of(series),
                   values["naming_template"], values["naming_template_no_volume"])

    def out(f):
        return CleanupFileOut(path=f.path, name=Path(f.path).name, size=f.size,
                              referenced=f.referenced, keep=f.keep)

    return CleanupPlanOut(
        groups=[CleanupGroupOut(label=g.label, files=[out(f) for f in g.files])
                for g in plan.groups],
        orphans=[out(f) for f in plan.orphans],
    )


@router.post("/series/{series_id}/cleanup", response_model=CleanupResultOut)
async def cleanup_apply(
    series_id: int, body: CleanupApplyIn, session: AsyncSession = Depends(get_session)
):
    from ..library.cleanup import apply_cleanup

    series = await _load(session, series_id)
    result = apply_cleanup(series, list(series.chapters), _folders_of(series), body.delete)
    await session.commit()
    return CleanupResultOut(
        deleted=result.deleted, repointed=result.repointed,
        skipped=result.skipped, freed_bytes=result.freed_bytes,
    )


@router.post("/series/{series_id}/files/map-range", response_model=FileMapRangeOut)
async def map_file_range(
    series_id: int, body: FileMapRangeIn, session: AsyncSession = Depends(get_session)
):
    """Map a whole-volume archive to a chapter range — the escape hatch for
    series whose metadata source lacks volume→chapter data. Also stamps the
    parsed volume onto those chapters so future scans/renames keep working."""
    from ..util import parse_volume_number

    series = await _load(session, series_id)
    if not Path(body.file_path).exists():
        raise HTTPException(400, "File not found on disk")
    lo, hi = sorted((body.from_number, body.to_number))
    volume = parse_volume_number(Path(body.file_path).stem)
    mapped = 0
    for ch in series.chapters:
        if lo <= ch.number <= hi:
            ch.downloaded = True
            ch.file_path = body.file_path
            if volume is not None:
                ch.volume = volume
            mapped += 1
    if mapped == 0:
        raise HTTPException(400, "No tracked chapters in that range")
    await session.commit()
    return FileMapRangeOut(mapped=mapped, volume=volume)


# --------------------------------------------------------------- folders

def _relative_to_root(root: Path, raw: str) -> str:
    """Store a path relative to the root when it's under it, else as given."""
    raw = raw.strip()
    if raw.startswith("/"):
        try:
            return str(Path(raw).relative_to(root))
        except ValueError:
            return raw
    return raw.strip("/")


@router.get("/series/{series_id}/folders", response_model=list[SeriesFolderOut])
async def list_folders(series_id: int, session: AsyncSession = Depends(get_session)):
    series = await _load(session, series_id)
    root = _root_of(series)
    out = [SeriesFolderOut(
        id=None, path=series.folder_name, resolved=str(series_dir(root, series)),
        primary=True, exists=series_dir(root, series).exists(),
    )]
    for f in series.extra_folders:
        p = root / f.path
        out.append(SeriesFolderOut(
            id=f.id, path=f.path, resolved=str(p), primary=False, exists=p.exists(),
        ))
    return out


@router.post("/series/{series_id}/folders", response_model=SeriesFolderOut, status_code=201)
async def add_folder(
    series_id: int, body: SeriesFolderIn, session: AsyncSession = Depends(get_session)
):
    series = await _load(session, series_id)
    root = _root_of(series)
    path = _relative_to_root(root, body.path)
    if not path or path == series.folder_name or any(f.path == path for f in series.extra_folders):
        raise HTTPException(400, "Folder already configured for this series")
    folder = SeriesFolder(series_id=series.id, path=path)
    session.add(folder)
    await session.commit()
    await session.refresh(folder)
    resolved = root / path
    return SeriesFolderOut(
        id=folder.id, path=folder.path, resolved=str(resolved),
        primary=False, exists=resolved.exists(),
    )


@router.delete("/series/{series_id}/folders/{folder_id}", status_code=204)
async def remove_folder(
    series_id: int, folder_id: int, session: AsyncSession = Depends(get_session)
):
    folder = await session.get(SeriesFolder, folder_id)
    if folder is None or folder.series_id != series_id:
        raise HTTPException(404, "Folder not found")
    await session.delete(folder)
    await session.commit()


# ---------------------------------------------------------- source links

@router.get("/sources", response_model=list[str])
async def list_sources():
    """Names of the direct sources that can be linked/searched."""
    return list(registry.DIRECT_SOURCES.keys())


@router.get("/series/{series_id}/sources/search", response_model=list[SourceCandidateOut])
async def source_search(
    series_id: int, source_name: str, query: str,
    session: AsyncSession = Depends(get_session),
):
    await _load(session, series_id)  # 404 if series missing
    values = await registry.apply_settings(session)  # noqa: F841 (configures sources)
    src = registry.DIRECT_SOURCES.get(source_name)
    if src is None:
        raise HTTPException(404, f"Unknown source {source_name!r}")
    try:
        candidates = await src.search_series(query)
    except Exception as exc:
        raise HTTPException(502, f"{source_name} search failed: {exc}") from exc
    return [
        SourceCandidateOut(source_name=source_name, external_id=c.external_id,
                           title=c.title, url=c.url, alt_titles=c.alt_titles)
        for c in candidates[:20]
    ]


@router.post("/series/{series_id}/sources", response_model=SourceLinkOut, status_code=201)
async def set_source_link(
    series_id: int, body: SourceLinkIn, session: AsyncSession = Depends(get_session)
):
    series = await _load(session, series_id)
    link = next((l for l in series.source_links if l.source_name == body.source_name), None)
    if link is None:
        link = SeriesSourceLink(source_name=body.source_name)
        series.source_links.append(link)
    link.external_id = body.external_id
    link.external_title = body.external_title
    link.external_url = body.external_url
    await session.commit()
    await session.refresh(link)
    return link


@router.delete("/series/{series_id}/sources/{link_id}", status_code=204)
async def delete_source_link(
    series_id: int, link_id: int, session: AsyncSession = Depends(get_session)
):
    link = await session.get(SeriesSourceLink, link_id)
    if link is None or link.series_id != series_id:
        raise HTTPException(404, "Source link not found")
    await session.delete(link)
    await session.commit()


@router.post("/series/{series_id}/resync", response_model=ResyncOut)
async def resync_chapters(series_id: int, session: AsyncSession = Depends(get_session)):
    """Rebuild the chapter list from the current source links (use after fixing
    a wrong link). Clears existing chapters + this series' download records,
    re-syncs from the corrected links, then re-adopts files from disk."""
    from sqlalchemy import delete as sa_delete

    from ..jobs.tasks import scan_series_folder, update_chapters
    from ..models import Download, HistoryEvent

    series = await _load(session, series_id)
    values = await registry.apply_settings(session)
    await session.execute(sa_delete(Download).where(Download.series_id == series_id))
    await session.execute(sa_delete(HistoryEvent).where(HistoryEvent.series_id == series_id))
    for ch in list(series.chapters):
        await session.delete(ch)
    await session.commit()

    series = await _load(session, series_id)
    await update_chapters(session, series, values)
    scan = await _scan_now(session, series)
    return ResyncOut(chapters=len(series.chapters), matched_chapters=scan)


def _run_resync(series: Series, chapters, volume_map: dict[float, int]):
    """Stamp `volume_map` onto `chapters` and re-point volume-archive file
    coverage: chapters are un-pointed from volume archives that don't match
    their (new) volume, then rescanned — exact chapter files win, matching
    archives cover the rest, and anything no longer backed by a file is
    cleared honestly. Mutates the given chapters in place (pass detached
    copies for a dry run); the caller commits.

    Returns (assigned, changed, repointed, cleared, diff) where diff is
    [(chapter number, old volume, new volume), …] sorted by chapter."""
    from ..util import has_chapter_marker, parse_volume_number

    changed = 0
    diff: list[tuple[float, int | None, int | None]] = []
    for ch in sorted(chapters, key=lambda c: c.number):
        new_volume = volume_map.get(ch.number)
        if ch.volume != new_volume:
            diff.append((ch.number, ch.volume, new_volume))
            ch.volume = new_volume
            changed += 1

    before: dict[int, str] = {}
    for ch in chapters:
        if not ch.downloaded or not ch.file_path:
            continue
        before[ch.id] = ch.file_path
        stem = Path(ch.file_path).stem
        file_volume = parse_volume_number(stem)
        if file_volume is not None and not has_chapter_marker(stem) \
                and file_volume != ch.volume:
            ch.downloaded = False
            ch.file_path = ""
    scan_series(series, list(chapters), _folders_of(series))

    repointed = sum(
        1 for ch in chapters
        if ch.downloaded and ch.file_path and before.get(ch.id, ch.file_path) != ch.file_path
    )
    cleared = sum(1 for ch in chapters if ch.id in before and not ch.downloaded)
    assigned = sum(1 for ch in chapters if ch.volume is not None)
    return assigned, changed, repointed, cleared, diff


def _chapter_copies(chapters):
    """Detached Chapter twins carrying just what _run_resync touches, so a
    dry run can't dirty the session."""
    from ..models import Chapter

    return [
        Chapter(id=ch.id, series_id=ch.series_id, number=ch.number,
                volume=ch.volume, downloaded=ch.downloaded, file_path=ch.file_path)
        for ch in chapters
    ]


@router.get("/series/{series_id}/volumes/resync-preview",
            response_model=VolumeResyncPreviewOut)
async def resync_volumes_preview(series_id: int, session: AsyncSession = Depends(get_session)):
    """Dry-run of the volume resync, one candidate per source with volume
    data: what an unqualified resync would rank (most complete sanitized map
    first — the first candidate is what it would apply), each with the counts
    and chapter-level diff applying it would produce. Nothing is written."""
    from ..jobs.tasks import collect_volume_maps, refine_volume_map_with_disk
    from ..volumes import sanitize_volume_map

    series = await _load(session, series_id)
    values = await registry.apply_settings(session)
    labeled = await collect_volume_maps(series, values)
    ranked = sorted(
        ((name, sanitize_volume_map(m)) for name, m in labeled),
        key=lambda nm: len(nm[1]), reverse=True,  # stable: priority order breaks ties
    )
    candidates = []
    for name, cleaned in ranked:
        if not cleaned:
            continue
        refined = refine_volume_map_with_disk(series, cleaned)
        assigned, changed, repointed, cleared, diff = _run_resync(
            series, _chapter_copies(series.chapters), refined
        )
        candidates.append(VolumeCandidateOut(
            source=name, map_size=len(cleaned),
            assigned=assigned, changed=changed, repointed=repointed, cleared=cleared,
            has_changes=bool(changed or repointed or cleared),
            diff=[VolumeDiffRowOut(number=n, old_volume=old, new_volume=new)
                  for n, old, new in diff],
        ))
    return VolumeResyncPreviewOut(candidates=candidates)


@router.post("/series/{series_id}/volumes/resync", response_model=VolumeResyncOut)
async def resync_volumes(
    series_id: int,
    body: VolumeResyncIn | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Rebuild every chapter's volume assignment from the most complete
    source's volume data (sanitized, applied verbatim — see mangarr.volumes),
    overwriting whatever is there — the fix for stale or wrongly-stamped
    assignments. Chapters the source can't place are distributed across the
    volume archives found on disk; with no matching files they stay
    unassigned. A specific source (as offered by the resync preview) can be
    requested instead of the auto-selected best map. No-op when no linked
    source has volume data (so manual mappings on metadata-gap series
    survive)."""
    from ..jobs.tasks import collect_volume_maps, fetch_volume_map, refine_volume_map_with_disk
    from ..volumes import sanitize_volume_map

    series = await _load(session, series_id)
    values = await registry.apply_settings(session)
    if body is not None and body.source:
        labeled = await collect_volume_maps(series, values)
        chosen = next((m for name, m in labeled if name == body.source), None)
        if chosen is None:
            raise HTTPException(404, f"No volume data from source {body.source!r}")
        volume_map = sanitize_volume_map(chosen)
    else:
        volume_map = await fetch_volume_map(series, values)
    if not volume_map:
        return VolumeResyncOut(has_data=False, assigned=0, changed=0,
                               repointed=0, cleared=0)

    # volume archives on disk anchor the chapters the source can't place
    volume_map = refine_volume_map_with_disk(series, volume_map)
    assigned, changed, repointed, cleared, _ = _run_resync(
        series, list(series.chapters), volume_map
    )
    await session.commit()
    return VolumeResyncOut(
        has_data=True, assigned=assigned, changed=changed,
        repointed=repointed, cleared=cleared,
    )


async def _scan_now(session: AsyncSession, series: Series) -> int:
    from ..library.scanner import scan_series
    result = scan_series(series, list(series.chapters), _folders_of(series))
    await session.commit()
    return result.matched_chapters


@router.post("/library/folder-preview", response_model=FolderPreviewOut)
async def folder_preview(body: FolderPreviewIn, session: AsyncSession = Depends(get_session)):
    """Which folder a prospective series would live in: the existing folder
    under the root whose name matches the title/alt-titles (the same adoption
    the scanner performs), or the default new folder derived from the title.
    Lets the add dialog show — and let the user correct — the mapping before
    the series is created."""
    from ..util import sanitize_filename

    root = await session.get(RootFolder, body.root_folder_id)
    if root is None:
        raise HTTPException(404, "Root folder not found")
    probe = Series(title=body.title, alt_titles="\n".join(body.alt_titles))
    found = find_existing_folder(Path(root.path), probe)
    default = sanitize_filename(body.title)
    name = found or default
    resolved = Path(root.path) / name
    return FolderPreviewOut(
        folder_name=name, path=str(resolved),
        exists=resolved.exists(), matched=found is not None,
        default_folder_name=default,
    )


# ---------------------------------------------------------- filesystem browse

@router.get("/filesystem", response_model=FilesystemListOut)
async def browse(
    path: str = Query(default=""), session: AsyncSession = Depends(get_session)
):
    """Folder browser (Sonarr-style, whole container filesystem). The empty
    path lists the configured root folders as shortcuts plus the filesystem
    root, so any mount — e.g. a downloads share — can be reached."""
    roots = [
        Path(r.path)
        for r in (await session.execute(select(RootFolder))).scalars().all()
    ]
    if not path:
        entries = [FilesystemEntryOut(name=str(r), path=str(r)) for r in roots]
        if not any(str(r) == "/" for r in roots):
            entries.append(FilesystemEntryOut(name="/", path="/"))
        return FilesystemListOut(path="", parent=None, entries=entries)
    target = Path(path)
    if not target.is_absolute():
        raise HTTPException(400, "Path must be absolute")
    if not target.is_dir():
        raise HTTPException(404, "Not a directory")
    try:
        children = [c for c in target.iterdir() if c.is_dir()]
    except OSError as exc:
        raise HTTPException(400, f"Cannot list {target}: {exc}") from exc
    entries = sorted(
        (FilesystemEntryOut(name=c.name, path=str(c)) for c in children),
        key=lambda e: e.name.lower(),
    )
    at_top = target == target.parent
    return FilesystemListOut(
        path=str(target),
        parent=None if at_top else str(target.parent),
        entries=entries,
    )
