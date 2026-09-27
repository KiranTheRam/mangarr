"""Bulk import of an existing library: list the folders under a root that no
series owns yet, match each to a metadata entry, and add the confirmed ones
in one go — each series keeps the folder it was matched from."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..adding import AddOptions, LibraryIndex, SeriesExists, create_series, start_refreshes
from ..db import get_session
from ..library.matcher import find_media_files
from ..library.scanner import resolve_folders
from ..models import RootFolder, Series
from ..schemas import (
    ImportFolderOut,
    ImportMatchOut,
    LibraryImportIn,
    LibraryImportResultOut,
)
from ..titles import plausible_title_match
from ..util import BRACKET_GROUPS, normalize_title
from .search import metadata_results

router = APIRouter(prefix="/library/import", tags=["library"])


def folder_query(name: str) -> str:
    """A folder name as a search query: release decorations ("(2019)",
    "[Digital]") and separator underscores removed."""
    cleaned = BRACKET_GROUPS.sub(" ", name).replace("_", " ")
    cleaned = " ".join(cleaned.split()).strip(" -.")
    return cleaned or name


async def _claimed_paths(session: AsyncSession) -> set[Path]:
    """Every directory some series already scans."""
    result = await session.execute(
        select(Series).options(selectinload(Series.root_folder), selectinload(Series.extra_folders))
    )
    claimed: set[Path] = set()
    for series in result.scalars().all():
        if series.root_folder is None:
            continue
        root = Path(series.root_folder.path)
        claimed.update(resolve_folders(root, series, [f.path for f in series.extra_folders]))
    return claimed


def _dir_identity(path: Path) -> tuple[int, int] | None:
    """(device, inode) of a directory: one folder reached through a symlink
    or a second mount point (a container mounting the same share at /manga
    and /media/manga) is still one folder."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _unclaimed_folders(root: Path, claimed: set[Path]) -> list[ImportFolderOut]:
    # a folder that is, or holds, a series folder belongs to that series —
    # so every existing ancestor of a claimed folder counts as taken too
    taken_paths: set[Path] = set()
    taken_ids: set[tuple[int, int]] = set()
    for path in claimed:
        for p in (path, *path.parents):
            if p in taken_paths:
                break
            taken_paths.add(p)
            if (identity := _dir_identity(p)) is not None:
                taken_ids.add(identity)
    out: list[ImportFolderOut] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child in taken_paths or _dir_identity(child) in taken_ids:
            continue
        out.append(ImportFolderOut(
            name=child.name,
            path=str(child),
            file_count=len(find_media_files(child)),
            query=folder_query(child.name),
        ))
    return out


@router.get("/folders", response_model=list[ImportFolderOut])
async def import_folders(root_folder_id: int, session: AsyncSession = Depends(get_session)):
    root = await session.get(RootFolder, root_folder_id)
    if root is None:
        raise HTTPException(404, "Root folder not found")
    root_path = Path(root.path)
    if not root_path.is_dir():
        raise HTTPException(400, f"Root folder {root.path} does not exist")
    claimed = await _claimed_paths(session)
    # walking a large library over a network share is slow, blocking I/O
    return await asyncio.to_thread(_unclaimed_folders, root_path, claimed)


def best_match(query: str, candidates) -> int | None:
    """Index of the candidate that clearly is the queried series: one of its
    titles normalizes to the query, or the top result is a close spelling
    variant. Anything looser is left for the user to confirm."""
    wanted = normalize_title(query)
    if not wanted:
        return None
    for i, cand in enumerate(candidates):
        titles = [cand.title, cand.english_title, *cand.alt_titles]
        if any(normalize_title(t) == wanted for t in titles if t):
            return i
    if candidates and any(
        plausible_title_match(t, query)
        for t in [candidates[0].title, candidates[0].english_title, *candidates[0].alt_titles]
        if t
    ):
        return 0
    return None


def _provider_ids(provider: str, provider_id: int) -> tuple[int | None, int | None]:
    """(anilist_id, mangaupdates_id) for one provider's id."""
    return (provider_id, None) if provider == "anilist" else (None, provider_id)


@router.get("/match", response_model=ImportMatchOut)
async def import_match(
    query: str = Query(min_length=1),
    provider: str = "mangaupdates",
    session: AsyncSession = Depends(get_session),
):
    try:
        candidates = await metadata_results(session, provider, query, limit=8)
    except Exception as exc:
        raise HTTPException(502, f"{provider} search failed: {exc}") from exc
    # metadata_results only compares this provider's ids; a series added
    # from the other provider (or never linked to this one) is still the
    # same manga, and importing its folder would add it twice
    library = await LibraryIndex.load(session)
    for cand in candidates:
        if not cand.in_library and library.find(
            *_provider_ids(cand.provider, int(cand.provider_id)),
            titles=[cand.title, cand.english_title, *cand.alt_titles],
        ) is not None:
            cand.in_library = True
    return ImportMatchOut(candidates=candidates, best=best_match(query, candidates))


@router.post("", response_model=list[LibraryImportResultOut])
async def import_library(body: LibraryImportIn, session: AsyncSession = Depends(get_session)):
    root = await session.get(RootFolder, body.root_folder_id)
    if root is None:
        raise HTTPException(404, "Root folder not found")
    if body.monitor_mode == "from_chapter" and body.monitor_from is None:
        raise HTTPException(422, "monitor_from is required for monitor_mode from_chapter")
    # a failed row rolls the session back, which expires `root`
    root_id = root.id
    library = await LibraryIndex.load(session)
    results: list[LibraryImportResultOut] = []
    added: list[int] = []
    for item in body.items:
        anilist_id, mangaupdates_id = _provider_ids(item.provider, item.provider_id)
        titles = [item.title, item.english_title, *item.alt_titles]
        existing = library.find(anilist_id, mangaupdates_id, titles)
        if existing is not None:
            results.append(LibraryImportResultOut(
                folder_name=item.folder_name, status="exists", series_id=existing,
                detail="already in the library",
            ))
            continue
        opts = AddOptions(
            root_folder_id=root_id,
            monitored=body.monitored,
            monitor_mode=body.monitor_mode,
            monitor_from=body.monitor_from,
            english_title=item.english_title,
            alt_titles=item.alt_titles,
            folder_name=item.folder_name,
            # the user matched this folder to this series on purpose
            folder_pinned=True,
        )
        try:
            series = await create_series(
                session, opts, anilist_id=anilist_id, mangaupdates_id=mangaupdates_id
            )
        except SeriesExists as exc:
            results.append(LibraryImportResultOut(
                folder_name=item.folder_name, status="exists", series_id=exc.series_id,
                detail="already in the library",
            ))
            continue
        except Exception as exc:  # one bad row (e.g. MetadataNotFound) must not sink the batch
            await session.rollback()
            results.append(LibraryImportResultOut(
                folder_name=item.folder_name, status="failed", detail=str(exc),
            ))
            continue
        added.append(series.id)
        # two folders of one batch matched to the same manga
        library.add(series.id, anilist_id, mangaupdates_id, [series.title, *titles])
        results.append(LibraryImportResultOut(
            folder_name=item.folder_name, status="added", series_id=series.id,
        ))
    start_refreshes(added, grab_missing=body.search_now)
    return results
