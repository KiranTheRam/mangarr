"""Detect and persist actionable metadata problems in library archives."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import stat
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from .comicinfo import (
    ArchiveChanged, ComicInfoError, ComicInfoMetadata, parse_comicinfo_int, read_comicinfo,
)
from .filename import parse_filename
from .library.scanner import (
    chapters_under_folders, resolve_folders, root_unavailable, unavailable_extra_folders,
)
from .models import Chapter, LibraryIssue, Series
from .titles import metadata_titles
from .util import normalize_title


# Manual mappings can share archives across series. Acquire this after the
# series lock and hold it from ownership validation through archive replacement.
manual_archive_lock = asyncio.Lock()


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
    archive_revision: str

    @property
    def revision(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def _key(series_id: int, file_path: str, field: str) -> str:
    raw = f"{series_id}\0{file_path}\0{field}".encode()
    return hashlib.sha256(raw).hexdigest()


def _numeric(value: str | None) -> float | None:
    try:
        number = float(value) if value is not None else None
        return number if number is not None and math.isfinite(number) else None
    except ValueError:
        return None


class ArchiveIneligible(ComicInfoError):
    """The file cannot be edited as a single chapter."""


class ArchiveOwnership:
    """Index the owners of each physical file, including aliases and hardlinks."""

    def __init__(self, paths: list[tuple[int, str]]):
        by_path = defaultdict(set)
        for chapter_id, raw_path in paths:
            by_path[raw_path].add(chapter_id)
        self.owners = defaultdict(set)
        self.unavailable: dict[str, OSError] = {}
        for raw_path, chapter_ids in by_path.items():
            try:
                file_stat = Path(raw_path).stat()
                self.owners[file_stat.st_dev, file_stat.st_ino].update(chapter_ids)
            except FileNotFoundError:
                continue
            except OSError as exc:
                self.unavailable[raw_path] = exc


async def chapter_paths(session: AsyncSession) -> list[tuple[int, str]]:
    rows = await session.execute(select(Chapter.id, Chapter.file_path).where(Chapter.file_path != ""))
    return [(chapter_id, path) for chapter_id, path in rows]


async def load_archive_ownership(session: AsyncSession) -> ArchiveOwnership:
    return await asyncio.to_thread(ArchiveOwnership, await chapter_paths(session))


def _archive_folders(series: Series) -> list[Path]:
    if series.root_folder is None:
        return []
    root = Path(series.root_folder.path)
    folders = resolve_folders(root, series, [folder.path for folder in series.extra_folders])
    absolute_extras = {Path(folder.path) for folder in series.extra_folders if Path(folder.path).is_absolute()}
    resolved_root = root.resolve()
    return [
        folder for raw in folders
        if (folder := raw.resolve()).is_relative_to(resolved_root) or raw in absolute_extras
    ]


def _inspect_chapter_archive(
    series: Series, chapter: Chapter, folders: list[Path], ownership: ArchiveOwnership
) -> tuple[Path, ComicInfoMetadata]:
    raw = Path(chapter.file_path)
    if chapter.excluded or not chapter.downloaded or raw.suffix.lower() not in {".cbz", ".zip"}:
        raise ArchiveIneligible("Chapter has no editable CBZ or ZIP archive")
    # Resolve the path before opening the ZIP, including when an alias points
    # to a file owned by another chapter or series.
    path = raw.resolve(strict=True)
    if not any(path.is_relative_to(folder) for folder in folders):
        raise ArchiveIneligible("Chapter file is outside the series folders")
    file_stat = path.stat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise ArchiveIneligible("Chapter archive must be a regular file")
    owners = ownership.owners.get((file_stat.st_dev, file_stat.st_ino), set())
    if chapter.id not in owners:
        raise ArchiveChanged("The chapter archive or its mapping changed; scan again")
    if len(owners) != 1:
        raise ArchiveIneligible("This archive has shared or uncertain chapter ownership")

    titles = {normalize_title(title) for title in metadata_titles(series)}
    for name in {raw.stem, path.stem}:
        coverage = parse_filename(name)
        if (
            coverage.ambiguous or coverage.is_range
            or (coverage.volume is not None and coverage.chapter is None)
            or (series.total_volumes == 1 and normalize_title(name) in titles)
        ):
            raise ArchiveIneligible("Whole-volume or multi-chapter archives need volume-level editing")
    info = read_comicinfo(path)
    if info.labels_multiple_chapters or metadata_identifies_volume(chapter, path, info.number, info.volume):
        raise ArchiveIneligible("Whole-volume or multi-chapter archives need volume-level editing")
    return path, info


def inspect_chapter_archive(
    series: Series, chapter: Chapter, paths: list[tuple[int, str]]
) -> tuple[Path, ComicInfoMetadata]:
    return _inspect_chapter_archive(series, chapter, _archive_folders(series), ArchiveOwnership(paths))


def preserves_chapter_number_mapping(chapter: Chapter, path: Path, number: str | None) -> bool:
    if _numeric(number) == chapter.number:
        return True
    # Match the library scanner's filename interpretation. If an alias and
    # its target differ, a scan may discover either name first.
    return all(
        parse_filename(name).chapter_number == chapter.number
        for name in {Path(chapter.file_path).stem, path.stem}
    )


def metadata_identifies_volume(
    chapter: Chapter, path: Path, number: str | None, volume: str | int | None,
) -> bool:
    volume_number = parse_comicinfo_int(str(volume)) if volume is not None else None
    return (
        volume_number is not None and volume_number >= 0
        and _numeric(number) is None
        and all(
            parse_filename(name).chapter_number is None
            for name in {Path(chapter.file_path).stem, path.stem}
        )
    )


def find_candidates(
    series: Series, ownership: ArchiveOwnership, expect_content: bool = True
) -> list[Candidate] | None:
    """Return None when an unavailable archive prevents a trustworthy scan."""
    if series.root_folder is None:
        return None
    root = Path(series.root_folder.path)
    if root_unavailable(root, expect_content):
        return None
    files: list[tuple[Chapter, Path, ComicInfoMetadata]] = []
    try:
        extras = [root / folder.path for folder in series.extra_folders]
        if chapters_under_folders(series.chapters, unavailable_extra_folders(extras)):
            return None
        folders = _archive_folders(series)
        for chapter in series.chapters:
            if chapter.excluded or not chapter.downloaded or not chapter.file_path:
                continue
            try:
                if chapter.file_path in ownership.unavailable:
                    raise ownership.unavailable[chapter.file_path]
                _, info = _inspect_chapter_archive(series, chapter, folders, ownership)
                files.append((chapter, Path(chapter.file_path), info))
            except FileNotFoundError:
                # An accessible parent lets us distinguish a deleted archive
                # from a missing mount. A configured extra may be its own mount.
                if not Path(chapter.file_path).parent.is_dir():
                    return None
            except ArchiveChanged:
                return None
            except ComicInfoError as exc:
                if isinstance(exc.__cause__, OSError):
                    return None
    except (OSError, RuntimeError):
        return None

    candidates: list[Candidate] = []
    uncertain_volume_sources = {"", "legacy", "disk-inferred", "comicinfo"}
    valid_titles = {normalized for value in metadata_titles(series) if (normalized := normalize_title(value))}

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
                info.revision,
            ))

        if info.page_count is not None:
            page_count = parse_comicinfo_int(info.page_count)
            if page_count != info.actual_page_count:
                candidates.append(Candidate(
                    chapter.id, "comicinfo_page_count_mismatch", str(path), "page_count",
                    info.page_count, str(info.actual_page_count),
                    ("ComicInfo page count is not a valid integer" if page_count is None
                     else "ComicInfo page count does not match the archive"),
                    f"ComicInfo declares {info.page_count!r}, but the archive contains "
                    f"{info.actual_page_count} image pages.",
                    info.revision,
                ))

        if info.series and valid_titles and normalize_title(info.series) not in valid_titles:
            candidates.append(Candidate(
                chapter.id, "comicinfo_series_mismatch", str(path), "series",
                info.series, series.title,
                "ComicInfo series name does not match this series",
                "Readers can split this archive into a separate series because its embedded "
                "series name is not one of Mangarr's known titles.",
                info.revision,
            ))

        if info.volume is not None:
            volume = parse_comicinfo_int(info.volume)
            reliable = chapter.volume_source not in uncertain_volume_sources
            expected_volume = parse_comicinfo_int(str(chapter.volume)) if chapter.volume is not None else None
            if volume is None:
                candidates.append(Candidate(
                    chapter.id, "comicinfo_volume_invalid", str(path), "volume",
                    info.volume, None,
                    "ComicInfo volume is not a valid integer",
                    "Readers expect the Volume field to be a 32-bit integer. Removing the bad "
                    "tag keeps the chapter available without inventing a volume.",
                    info.revision,
                ))
            elif chapter.volume is None:
                candidates.append(Candidate(
                    chapter.id, "comicinfo_volume_unexpected", str(path), "volume",
                    info.volume, None,
                    f"ComicInfo creates Volume {info.volume} for an unassigned chapter",
                    "Mangarr has no volume assignment for this chapter, but the archive tag "
                    "makes readers create one. The recommended change removes only the tag.",
                    info.revision,
                ))
            elif reliable and expected_volume is not None and volume != expected_volume:
                candidates.append(Candidate(
                    chapter.id, "comicinfo_volume_mismatch", str(path), "volume",
                    info.volume, str(expected_volume),
                    f"ComicInfo volume disagrees with {chapter.volume_source}",
                    f"The archive says Volume {info.volume}, while {chapter.volume_source} "
                    f"places chapter {chapter.number:g} in Volume {chapter.volume}.",
                    info.revision,
                ))

    # A single loose-chapter archive declaring a volume is especially risky:
    # Kavita creates that volume even if every sibling file is untagged. This
    # catches imported one-off bonus chapters without pretending disk-inferred
    # assignments are authoritative enough to validate the bad tag.
    by_volume: dict[int, list[tuple[Chapter, Path, ComicInfoMetadata]]] = {}
    for item in files:
        if item[0].volume is not None:
            by_volume.setdefault(item[0].volume, []).append(item)
    for volume, peers in by_volume.items():
        declared = [item for item in peers if item[2].volume is not None]
        if len(peers) < 2 or len(declared) != 1:
            continue
        chapter, path, info = declared[0]
        if (
            parse_comicinfo_int(info.volume) != volume
            or chapter.volume_locked
            or chapter.volume_source not in uncertain_volume_sources
        ):
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
            info.revision,
        ))

    return candidates


async def current_candidates(
    session: AsyncSession, series: Series, ownership: ArchiveOwnership | None = None,
) -> list[Candidate] | None:
    # A scan can infer new chapter rows immediately before detection; assign
    # their ids so issues link to remediation on the first pass.
    from .jobs.tasks import root_expects_content

    await session.flush()
    unloaded = inspect(series).unloaded & {"chapters", "root_folder", "extra_folders"}
    if unloaded:
        await session.refresh(series, list(unloaded))
    if series.root_folder is None:
        return None
    expect_content = await root_expects_content(session, series)
    if ownership is None:
        ownership = await load_archive_ownership(session)
    return await asyncio.to_thread(find_candidates, series, ownership, expect_content)


async def detect_series_issues(
    session: AsyncSession, series: Series, ownership: ArchiveOwnership | None = None,
) -> list[LibraryIssue]:
    """Reconcile under the caller's series lock, held through the commit."""
    candidates = await current_candidates(session, series, ownership)
    if candidates is None:
        return []
    return await reconcile_candidates(session, series.id, candidates)


async def reconcile_candidates(
    session: AsyncSession, series_id: int, candidates: list[Candidate]
) -> list[LibraryIssue]:
    existing = list((await session.execute(
        select(LibraryIssue).where(LibraryIssue.series_id == series_id)
    )).scalars())
    by_key = {issue.key: issue for issue in existing}
    active_keys: set[str] = set()
    now = datetime.now(timezone.utc)
    result: list[LibraryIssue] = []
    for candidate in candidates:
        key = _key(series_id, candidate.file_path, candidate.field)
        active_keys.add(key)
        issue = by_key.get(key)
        if issue is None:
            issue = LibraryIssue(key=key, series_id=series_id, ignored=False)
            session.add(issue)
        elif issue.kind != candidate.kind:
            # A dismissal (including a pending Ignore request) belongs to the
            # previous warning, even when the archive and field are the same.
            issue.ignored = False
            issue.public_id = uuid4().hex
        revision = candidate.revision
        if issue.revision != revision or issue.chapter_id != candidate.chapter_id:
            values = asdict(candidate)
            values.pop("archive_revision")
            for name, value in values.items():
                setattr(issue, name, value)
            issue.revision = revision
            issue.updated_at = now
        result.append(issue)
    for issue in existing:
        if issue.key not in active_keys:
            await session.delete(issue)
    await session.flush()
    return result
