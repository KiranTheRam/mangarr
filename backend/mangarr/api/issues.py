"""Library metadata issue listing, dismissal, and one-click remediation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from .. import settings_service
from ..comicinfo import (
    ArchiveChanged, ComicInfoError, ComicInfoMetadata, parse_comicinfo_int, update_comicinfo,
)
from ..db import get_session
from ..jobs.tasks import series_lock
from ..metadata_issues import (
    ArchiveIneligible, chapter_paths, current_candidates, detect_series_issues,
    filename_conflicts_with_mapping, inspect_chapter_archive, load_archive_ownership, manual_archive_lock,
    metadata_identifies_volume, preserves_chapter_number_mapping, reconcile_candidates,
)
from ..models import Chapter, LibraryIssue, Series
from ..schemas import (
    ComicInfoOut,
    ComicInfoUpdateIn,
    IssueResolveIn,
    IssueScanOut,
    LibraryIssueOut,
)

router = APIRouter(tags=["issues"])


async def _series(session: AsyncSession, series_id: int) -> Series | None:
    return (await session.execute(
        select(Series)
        .options(
            selectinload(Series.chapters),
            selectinload(Series.root_folder),
            selectinload(Series.extra_folders),
        )
        .where(Series.id == series_id, Series.deletion_pending.is_(False))
    )).scalar_one_or_none()


async def _inspect(
    session: AsyncSession, series: Series, chapter_id: int
) -> tuple[Chapter, Path, ComicInfoMetadata]:
    chapter = next((item for item in series.chapters if item.id == chapter_id), None)
    if chapter is None:
        raise HTTPException(404, "Chapter not found")
    try:
        path, info = await run_in_threadpool(
            inspect_chapter_archive, series, chapter, await chapter_paths(session),
        )
        return chapter, path, info
    except ArchiveIneligible as exc:
        raise HTTPException(400, str(exc)) from exc
    except ArchiveChanged as exc:
        raise HTTPException(409, str(exc)) from exc
    except ComicInfoError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(400, "Chapter file is not available") from exc


def _normalized(value: str | int | None) -> str | None:
    return " ".join(str(value).split()) or None if value is not None else None


async def _write_archive(
    series: Series, chapter: Chapter, path: Path, current: ComicInfoMetadata,
    updates: dict[str, str | int | None],
) -> ComicInfoMetadata:
    value = updates.get("number", current.number)
    number = str(value) if value is not None else None
    tracked_numbers = {item.number for item in series.chapters if not item.excluded}
    disputed = filename_conflicts_with_mapping(chapter, path, tracked_numbers)
    if disputed:
        # The archive may belong to another chapter (or series), so only
        # archive-intrinsic edits are safe: removing an invalid Volume tag is.
        for field in ("series", "volume"):
            if field not in updates or _normalized(updates[field]) == _normalized(getattr(current, field)):
                continue
            if field == "volume" and updates[field] is None and parse_comicinfo_int(current.volume) is None:
                continue
            raise HTTPException(
                409,
                "The filename identifies a different tracked chapter. Correct the file mapping "
                f"or rename the archive before changing ComicInfo {field.title()}.",
            )
    if number != current.number:
        if disputed:
            raise HTTPException(
                409,
                "The filename identifies a different tracked chapter. Correct the file mapping "
                "or rename the archive before changing ComicInfo Number.",
            )
        if not preserves_chapter_number_mapping(chapter, path, number):
            raise HTTPException(
                409,
                "Mangarr uses ComicInfo Number to match this file. Keep it set to "
                f"chapter {chapter.number:g} or rename the file to identify that chapter before editing.",
            )
    if metadata_identifies_volume(chapter, path, number, updates.get("volume", current.volume)):
        raise HTTPException(
            409,
            "Set ComicInfo Number to "
            f"chapter {chapter.number:g} or rename the file to identify that chapter before setting Volume.",
        )
    try:
        return await run_in_threadpool(update_comicinfo, path, updates, expected_revision=current.revision)
    except ArchiveChanged as exc:
        raise HTTPException(409, str(exc)) from exc
    except ComicInfoError as exc:
        raise HTTPException(422, str(exc)) from exc


async def _notify_reader(session: AsyncSession, series: Series) -> None:
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
    async with series_lock(series_id):
        series = await _series(session, series_id)
        if series is None:
            raise HTTPException(404, "Series not found")
        _, path, info = await _inspect(session, series, chapter_id)
        return ComicInfoOut(file_path=str(path), **info.__dict__)


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
    updates = body.model_dump(exclude={"revision"}, exclude_unset=True)
    if not updates:
        raise HTTPException(422, "No ComicInfo fields to update")
    async with series_lock(series_id), manual_archive_lock:
        series = await _series(session, series_id)
        if series is None:
            raise HTTPException(404, "Series not found")
        chapter, path, current = await _inspect(session, series, chapter_id)
        if current.revision != body.revision:
            raise HTTPException(409, "The chapter archive changed; reopen it and review the metadata")
        info = await _write_archive(series, chapter, path, current, updates)
        await detect_series_issues(session, series)
        await session.commit()
        await _notify_reader(session, series)
        return ComicInfoOut(file_path=str(path), **info.__dict__)


def _issue_out(issue: LibraryIssue, series_title: str, chapter_number: float | None):
    return LibraryIssueOut(
        id=issue.public_id,
        revision=issue.revision,
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
        .outerjoin(
            Chapter, (Chapter.id == LibraryIssue.chapter_id) & (Chapter.series_id == LibraryIssue.series_id),
        )
        .where(LibraryIssue.ignored == ignored, Series.deletion_pending.is_(False))  # noqa: E712
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
    series_ids = list((await session.scalars(select(Series.id).where(Series.deletion_pending.is_(False)))).all())
    ownership = await load_archive_ownership(session)
    await session.commit()
    for series_id in series_ids:
        async with series_lock(series_id):
            series = await _series(session, series_id)
            if series is not None:
                await detect_series_issues(session, series, ownership)
            await session.commit()
    counts = dict((await session.execute(
        select(LibraryIssue.ignored, func.count(LibraryIssue.id)).group_by(LibraryIssue.ignored)
    )).all())
    return IssueScanOut(active=counts.get(False, 0), ignored=counts.get(True, 0))


@asynccontextmanager
async def _locked_issue(session: AsyncSession, issue_id: str):
    series_id = await session.scalar(
        select(LibraryIssue.series_id)
        .join(Series, Series.id == LibraryIssue.series_id)
        .where(LibraryIssue.public_id == issue_id, Series.deletion_pending.is_(False))
    )
    if series_id is None:
        raise HTTPException(404, "Issue not found")
    async with series_lock(series_id):
        issue = await session.scalar(
            select(LibraryIssue)
            .join(Series, Series.id == LibraryIssue.series_id)
            .where(LibraryIssue.public_id == issue_id, Series.deletion_pending.is_(False))
        )
        if issue is None:
            raise HTTPException(404, "Issue not found")
        yield issue


@router.post("/issues/{issue_id}/ignore", status_code=204)
async def ignore_issue(issue_id: str, session: AsyncSession = Depends(get_session)):
    async with _locked_issue(session, issue_id) as issue:
        issue.ignored = True
        await session.commit()


@router.post("/issues/{issue_id}/restore", status_code=204)
async def restore_issue(issue_id: str, session: AsyncSession = Depends(get_session)):
    async with _locked_issue(session, issue_id) as issue:
        issue.ignored = False
        await session.commit()


@router.post("/issues/{issue_id}/resolve", status_code=204)
async def resolve_issue(
    issue_id: str, body: IssueResolveIn, session: AsyncSession = Depends(get_session)
):
    async with _locked_issue(session, issue_id) as issue, manual_archive_lock:
        if issue.ignored or body.revision != issue.revision:
            raise HTTPException(409, "The issue changed; review the current recommendation")
        series = await _series(session, issue.series_id)
        if series is None:
            raise HTTPException(409, "The affected series no longer exists")
        candidates = await current_candidates(session, series)
        if candidates is None:
            raise HTTPException(409, "The library files are unavailable; try again when they are accessible")
        candidate = next((
            item for item in candidates if item.chapter_id == issue.chapter_id
            and item.file_path == issue.file_path and item.field == issue.field
        ), None)
        if candidate is None or candidate.revision != body.revision:
            await reconcile_candidates(session, series.id, candidates)
            await session.commit()
            raise HTTPException(409, "The issue changed; review the current recommendation")
        chapter, path, info = await _inspect(session, series, candidate.chapter_id)
        if info.revision != candidate.archive_revision:
            raise HTTPException(409, "The chapter archive changed; scan issues again")
        await _write_archive(series, chapter, path, info, {candidate.field: candidate.suggested_value})
        await session.delete(issue)
        await detect_series_issues(session, series)
        await session.commit()
        await _notify_reader(session, series)
