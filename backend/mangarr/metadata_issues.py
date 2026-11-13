"""Detect and persist actionable metadata problems in library archives."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .comicinfo import ComicInfoError, ComicInfoMetadata, read_comicinfo
from .models import Chapter, LibraryIssue, Series
from .titles import metadata_titles
from .util import normalize_title


@dataclass(frozen=True)
class Candidate:
    chapter_id: int
    kind: str
    file_path: str
    field: str
    current_value: str | None
    suggested_value: str | None
    title: str
    detail: str


def _key(series_id: int, file_path: str, field: str) -> str:
    raw = f"{series_id}\0{file_path}\0{field}".encode()
    return hashlib.sha256(raw).hexdigest()


def _numeric(value: str | None) -> float | None:
    try:
        number = float(value) if value is not None else None
        return number if number is not None and math.isfinite(number) else None
    except ValueError:
        return None


def _exact_files(series: Series) -> list[tuple[Chapter, Path, ComicInfoMetadata]]:
    by_path: dict[str, list[Chapter]] = {}
    for chapter in series.chapters:
        if chapter.excluded or not chapter.downloaded or not chapter.file_path:
            continue
        by_path.setdefault(chapter.file_path, []).append(chapter)
    found: list[tuple[Chapter, Path, ComicInfoMetadata]] = []
    for raw_path, chapters in by_path.items():
        if len(chapters) != 1:
            continue  # whole-volume archives need volume-level remediation
        path = Path(raw_path)
        if path.suffix.lower() not in {".cbz", ".zip"} or not path.is_file():
            continue
        try:
            found.append((chapters[0], path, read_comicinfo(path)))
        except ComicInfoError:
            continue
    return found


def find_candidates(series: Series) -> list[Candidate]:
    files = _exact_files(series)
    candidates: list[Candidate] = []
    claimed: set[tuple[str, str]] = set()

    for chapter, path, info in files:
        if not info.exists:
            continue
        number = _numeric(info.number)
        if info.number is not None and (
            number is None or not math.isclose(number, chapter.number, abs_tol=1e-6)
        ):
            chapter_number = float(chapter.number)
            suggested = str(int(chapter_number)) if chapter_number.is_integer() else str(chapter_number)
            candidates.append(Candidate(
                chapter.id, "comicinfo_number_mismatch", str(path), "number",
                info.number, suggested,
                f"ComicInfo chapter number does not match chapter {suggested}",
                "The archive filename and Mangarr mapping identify a different chapter. "
                "Readers may sort or label this file incorrectly.",
            ))
            claimed.add((str(path), "number"))

        if info.page_count is not None:
            page_count = _numeric(info.page_count)
            if page_count is None or page_count != info.actual_page_count:
                candidates.append(Candidate(
                    chapter.id, "comicinfo_page_count_mismatch", str(path), "page_count",
                    info.page_count, str(info.actual_page_count),
                    "ComicInfo page count does not match the archive",
                    f"ComicInfo declares {info.page_count!r}, but the archive contains "
                    f"{info.actual_page_count} image pages.",
                ))
                claimed.add((str(path), "page_count"))

        valid_titles = {
            normalized
            for value in metadata_titles(series)
            if (normalized := normalize_title(value))
        }
        if info.series and valid_titles and normalize_title(info.series) not in valid_titles:
            candidates.append(Candidate(
                chapter.id, "comicinfo_series_mismatch", str(path), "series",
                info.series, series.title,
                "ComicInfo series name does not match this series",
                "Readers can split this archive into a separate series because its embedded "
                "series name is not one of Mangarr's known titles.",
            ))
            claimed.add((str(path), "series"))

        if info.volume is not None:
            volume = _numeric(info.volume)
            reliable = chapter.volume_source not in {"", "legacy", "disk-inferred", "comicinfo"}
            if volume is None or not volume.is_integer():
                candidates.append(Candidate(
                    chapter.id, "comicinfo_volume_invalid", str(path), "volume",
                    info.volume, None,
                    "ComicInfo volume is not a whole number",
                    "Readers expect the Volume field to be a whole number. Removing the bad "
                    "tag keeps the chapter available without inventing a volume.",
                ))
                claimed.add((str(path), "volume"))
            elif chapter.volume is None:
                candidates.append(Candidate(
                    chapter.id, "comicinfo_volume_unexpected", str(path), "volume",
                    info.volume, None,
                    f"ComicInfo creates Volume {info.volume} for an unassigned chapter",
                    "Mangarr has no volume assignment for this chapter, but the archive tag "
                    "makes readers create one. The recommended change removes only the tag.",
                ))
                claimed.add((str(path), "volume"))
            elif reliable and int(volume) != chapter.volume:
                candidates.append(Candidate(
                    chapter.id, "comicinfo_volume_mismatch", str(path), "volume",
                    info.volume, str(chapter.volume),
                    f"ComicInfo volume disagrees with {chapter.volume_source}",
                    f"The archive says Volume {info.volume}, while {chapter.volume_source} "
                    f"places chapter {chapter.number:g} in Volume {chapter.volume}.",
                ))
                claimed.add((str(path), "volume"))

    # A single loose-chapter archive declaring a volume is especially risky:
    # Kavita creates that volume even if every sibling file is untagged. This
    # catches imported one-off bonus chapters without pretending disk-inferred
    # assignments are authoritative enough to validate the bad tag.
    by_volume: dict[int, list[tuple[Chapter, Path, ComicInfoMetadata]]] = {}
    for item in files:
        if item[0].volume is not None:
            by_volume.setdefault(item[0].volume, []).append(item)
    for volume, peers in by_volume.items():
        declared = [item for item in peers if _numeric(item[2].volume) == float(volume)]
        if len(peers) < 2 or len(declared) != 1:
            continue
        chapter, path, info = declared[0]
        if (str(path), "volume") in claimed:
            continue
        untagged = len(peers) - 1
        candidates.append(Candidate(
            chapter.id, "comicinfo_volume_isolated", str(path), "volume",
            info.volume, None,
            f"Only this archive declares Volume {volume}",
            f"This chapter is the only file among {len(peers)} downloaded chapters assigned "
            f"to Volume {volume} that embeds a Volume tag; {untagged} sibling file"
            f"{'s are' if untagged != 1 else ' is'} untagged. Kavita will create Volume "
            f"{volume} from this file alone. Remove the tag unless that grouping is intentional.",
        ))

    return candidates


async def detect_series_issues(session: AsyncSession, series: Series) -> list[LibraryIssue]:
    # A scan can infer new chapter rows immediately before detection; assign
    # their ids so issues link to remediation on the first pass.
    await session.flush()
    candidates = find_candidates(series)
    existing = list((await session.execute(
        select(LibraryIssue).where(LibraryIssue.series_id == series.id)
    )).scalars())
    by_key = {issue.key: issue for issue in existing}
    active_keys: set[str] = set()
    now = datetime.now(timezone.utc)
    result: list[LibraryIssue] = []
    for candidate in candidates:
        key = _key(series.id, candidate.file_path, candidate.field)
        active_keys.add(key)
        issue = by_key.get(key)
        if issue is None:
            issue = LibraryIssue(key=key, series_id=series.id, ignored=False)
            session.add(issue)
        issue.chapter_id = candidate.chapter_id
        issue.kind = candidate.kind
        issue.file_path = candidate.file_path
        issue.field = candidate.field
        issue.current_value = candidate.current_value
        issue.suggested_value = candidate.suggested_value
        issue.title = candidate.title
        issue.detail = candidate.detail
        issue.updated_at = now
        result.append(issue)
    for issue in existing:
        if issue.key not in active_keys:
            await session.delete(issue)
    await session.flush()
    return result
