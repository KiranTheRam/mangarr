"""Library metadata issue listing, dismissal, and one-click remediation."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .. import settings_service
from ..comicinfo import ComicInfoError, read_comicinfo, update_comicinfo
from ..db import get_session
from ..library.scanner import resolve_folders
from ..metadata_issues import detect_series_issues
from ..models import Chapter, LibraryIssue, Series
from ..schemas import (
    ComicInfoOut,
    ComicInfoUpdateIn,
    IssueScanOut,
    LibraryIssueOut,
)

router = APIRouter(tags=["issues"])


async def _series(session: AsyncSession, series_id: int) -> Series:
    series = (await session.execute(
        select(Series)
        .options(
            selectinload(Series.chapters),
            selectinload(Series.root_folder),
            selectinload(Series.extra_folders),
        )
        .where(Series.id == series_id)
    )).scalar_one_or_none()
    if series is None:
        raise HTTPException(404, "Series not found")
    return series


def _contained_file(series: Series, chapter: Chapter) -> Path:
    if series.root_folder is None or not chapter.file_path:
        raise HTTPException(400, "Chapter has no editable file")
    try:
        path = Path(chapter.file_path).resolve(strict=True)
        folders = resolve_folders(
            Path(series.root_folder.path), series, [folder.path for folder in series.extra_folders]
        )
        if not any(path.is_relative_to(folder.resolve(strict=False)) for folder in folders):
            raise HTTPException(400, "Chapter file is outside the series folders")
    except OSError as exc:
        raise HTTPException(400, "Chapter file is not available") from exc
    if path.suffix.lower() not in {".cbz", ".zip"}:
        raise HTTPException(400, "ComicInfo editing is supported for CBZ and ZIP files")
    sharing = [
        item for item in series.chapters
        if not item.excluded and item.file_path == chapter.file_path
    ]
    if len(sharing) != 1:
        raise HTTPException(400, "Edit whole-volume archive metadata from its volume workflow")
    return path


def _comicinfo_out(path: Path) -> ComicInfoOut:
    try:
        info = read_comicinfo(path)
    except ComicInfoError as exc:
        raise HTTPException(422, str(exc)) from exc
    return ComicInfoOut(file_path=str(path), **info.__dict__)


async def _notify_reader(session: AsyncSession, series: Series) -> None:
    # Reuse the normal debounced Kavita notification path; a disabled or
    # unconfigured integration is a no-op.
    from ..jobs.tasks import _notify_kavita

    values = await settings_service.get_all(session)
    _notify_kavita(values, series)


@router.get(
    "/series/{series_id}/chapters/{chapter_id}/comicinfo",
    response_model=ComicInfoOut,
)
async def get_chapter_comicinfo(
    series_id: int, chapter_id: int, session: AsyncSession = Depends(get_session)
):
    series = await _series(session, series_id)
    chapter = next((item for item in series.chapters if item.id == chapter_id), None)
    if chapter is None:
        raise HTTPException(404, "Chapter not found")
    return _comicinfo_out(_contained_file(series, chapter))


@router.put(
    "/series/{series_id}/chapters/{chapter_id}/comicinfo",
    response_model=ComicInfoOut,
)
async def update_chapter_comicinfo(
    series_id: int,
    chapter_id: int,
    body: ComicInfoUpdateIn,
    session: AsyncSession = Depends(get_session),
):
    series = await _series(session, series_id)
    chapter = next((item for item in series.chapters if item.id == chapter_id), None)
    if chapter is None:
        raise HTTPException(404, "Chapter not found")
    path = _contained_file(series, chapter)
    try:
        update_comicinfo(path, body.model_dump())
    except ComicInfoError as exc:
        raise HTTPException(422, str(exc)) from exc
    await detect_series_issues(session, series)
    await session.commit()
    await _notify_reader(session, series)
    return _comicinfo_out(path)


def _issue_out(issue: LibraryIssue, series_title: str, chapter_number: float | None):
    return LibraryIssueOut(
        id=issue.id,
        series_id=issue.series_id,
        series_title=series_title,
        chapter_id=issue.chapter_id,
        chapter_number=chapter_number,
        kind=issue.kind,
        file_path=issue.file_path,
        field=issue.field,
        current_value=issue.current_value,
        suggested_value=issue.suggested_value,
        title=issue.title,
        detail=issue.detail,
        ignored=issue.ignored,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
    )


@router.get("/issues", response_model=list[LibraryIssueOut])
async def list_issues(
    ignored: bool = Query(False),
    series_id: int | None = Query(None),
    session: AsyncSession = Depends(get_session),
):
    query = (
        select(LibraryIssue, Series.title, Chapter.number)
        .join(Series, Series.id == LibraryIssue.series_id)
        .outerjoin(Chapter, Chapter.id == LibraryIssue.chapter_id)
        .where(LibraryIssue.ignored == ignored)  # noqa: E712
        .order_by(LibraryIssue.updated_at.desc(), LibraryIssue.id.desc())
    )
    if series_id is not None:
        query = query.where(LibraryIssue.series_id == series_id)
    return [
        _issue_out(issue, title, number)
        for issue, title, number in (await session.execute(query)).all()
    ]


@router.post("/issues/scan", response_model=IssueScanOut)
async def scan_issues(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Series).options(selectinload(Series.chapters))
    )
    for series in result.scalars().unique():
        await detect_series_issues(session, series)
    await session.commit()
    counts = dict((await session.execute(
        select(LibraryIssue.ignored, func.count(LibraryIssue.id)).group_by(LibraryIssue.ignored)
    )).all())
    return IssueScanOut(active=counts.get(False, 0), ignored=counts.get(True, 0))


@router.post("/issues/{issue_id}/ignore", status_code=204)
async def ignore_issue(issue_id: int, session: AsyncSession = Depends(get_session)):
    issue = await session.get(LibraryIssue, issue_id)
    if issue is None:
        raise HTTPException(404, "Issue not found")
    issue.ignored = True
    await session.commit()


@router.post("/issues/{issue_id}/restore", status_code=204)
async def restore_issue(issue_id: int, session: AsyncSession = Depends(get_session)):
    issue = await session.get(LibraryIssue, issue_id)
    if issue is None:
        raise HTTPException(404, "Issue not found")
    issue.ignored = False
    await session.commit()


@router.post("/issues/{issue_id}/resolve", status_code=204)
async def resolve_issue(issue_id: int, session: AsyncSession = Depends(get_session)):
    issue = await session.get(LibraryIssue, issue_id)
    if issue is None:
        raise HTTPException(404, "Issue not found")
    series = await _series(session, issue.series_id)
    chapter = next((item for item in series.chapters if item.id == issue.chapter_id), None)
    if chapter is None:
        raise HTTPException(409, "The affected chapter no longer exists")
    path = _contained_file(series, chapter)
    if str(path) != str(Path(issue.file_path).resolve(strict=False)):
        raise HTTPException(409, "The affected file changed; scan issues again")
    try:
        info = read_comicinfo(path)
        if info.value(issue.field) != issue.current_value:
            await detect_series_issues(session, series)
            await session.commit()
            raise HTTPException(409, "The metadata changed; review the refreshed issue")
        update_comicinfo(path, {issue.field: issue.suggested_value})
    except ComicInfoError as exc:
        raise HTTPException(422, str(exc)) from exc
    await detect_series_issues(session, series)
    await session.commit()
    await _notify_reader(session, series)
