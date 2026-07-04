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
from ..library.scanner import find_existing_folder, scan_series, series_dir
from ..models import RootFolder, Series
from ..schemas import (
    FileMapIn,
    FilesystemEntryOut,
    FilesystemListOut,
    RenameApplyIn,
    RenameItemOut,
    RenameOutcomeOut,
    ScanResultOut,
    SeriesFileOut,
)

router = APIRouter(tags=["library"])


async def _load(session: AsyncSession, series_id: int) -> Series:
    result = await session.execute(
        select(Series)
        .options(selectinload(Series.chapters), selectinload(Series.root_folder))
        .where(Series.id == series_id)
    )
    series = result.scalar_one_or_none()
    if series is None:
        raise HTTPException(404, "Series not found")
    return series


def _folder_of(series: Series) -> Path:
    if series.root_folder is None:
        raise HTTPException(400, "Series has no root folder configured")
    return series_dir(Path(series.root_folder.path), series)


def _within(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------ scan

@router.post("/series/{series_id}/scan", response_model=ScanResultOut)
async def scan(series_id: int, session: AsyncSession = Depends(get_session)):
    series = await _load(session, series_id)
    root = Path(series.root_folder.path) if series.root_folder else None
    if root is None:
        raise HTTPException(400, "Series has no root folder configured")
    folder = _folder_of(series)
    if not folder.exists():
        found = find_existing_folder(root, series)
        if found:
            series.folder_name = found
            folder = root / found
    result = scan_series(series, list(series.chapters), folder)
    await session.commit()
    return ScanResultOut(
        folder=str(folder),
        folder_exists=folder.exists(),
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
        series, list(series.chapters), _folder_of(series),
        values["naming_template"], values["naming_template_no_volume"],
    )


@router.get("/series/{series_id}/rename", response_model=list[RenameItemOut])
async def rename_preview(series_id: int, session: AsyncSession = Depends(get_session)):
    series = await _load(session, series_id)
    return [
        RenameItemOut(
            chapter_ids=i.chapter_ids, current_path=i.current_path,
            current_name=i.current_name, new_path=i.new_path, new_name=i.new_name,
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
    folder = _folder_of(series)
    if not folder.exists():
        return []
    result = match_files(find_media_files(folder), list(series.chapters))
    out: list[SeriesFileOut] = []
    for mf in result.matched:
        out.append(SeriesFileOut(
            path=str(mf.media.path), name=mf.media.path.name, is_dir=mf.media.is_dir,
            chapter_number=mf.media.chapter_number, volume_number=mf.media.volume_number,
            matched_chapter_id=mf.chapter.id if mf.chapter else None,
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


# ---------------------------------------------------------- filesystem browse

@router.get("/filesystem", response_model=FilesystemListOut)
async def browse(
    path: str = Query(default=""), session: AsyncSession = Depends(get_session)
):
    roots = [
        Path(r.path)
        for r in (await session.execute(select(RootFolder))).scalars().all()
    ]
    if not path:
        return FilesystemListOut(
            path="", parent=None,
            entries=[FilesystemEntryOut(name=str(r), path=str(r)) for r in roots],
        )
    target = Path(path)
    if not any(_within(target, r) for r in roots):
        raise HTTPException(400, "Path is outside the configured root folders")
    if not target.is_dir():
        raise HTTPException(404, "Not a directory")
    entries = sorted(
        (FilesystemEntryOut(name=c.name, path=str(c))
         for c in target.iterdir() if c.is_dir()),
        key=lambda e: e.name.lower(),
    )
    is_root = any(target == r for r in roots)
    return FilesystemListOut(
        path=str(target),
        parent=None if is_root else str(target.parent),
        entries=entries,
    )
