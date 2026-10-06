"""Scan existing library folders and adopt files in place.

Read-only with respect to the filesystem: it only records which tracked
chapters are already present on disk (setting Chapter.downloaded/file_path).
It never copies, moves, or writes files — that's what lets mangarr sit on top
of a library the user already has without re-downloading anything.

A series can have several folders (a primary plus extras), e.g. a volumes
directory and a separate loose-chapters directory; scanning looks across all
of them and, where a chapter is available both as a loose file and inside a
whole-volume archive, the exact chapter file wins."""

import logging
import math
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..chapter_metadata import apply_title
from ..models import Chapter, Series
from ..util import BRACKET_GROUPS, normalize_title
from .matcher import (
    MediaFile,
    comicinfo_number,
    comicinfo_title,
    find_series_media_files,
    match_files,
)
from .naming import series_folder

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    matched_chapters: int = 0  # chapters newly marked owned
    added_chapters: int = 0  # explicit on-disk decimal/one-volume rows created
    volume_files: int = 0  # whole-volume archives found
    cleared: int = 0  # chapters whose recorded file vanished
    unmatched: list[MediaFile] = field(default_factory=list)

    @property
    def unmatched_count(self) -> int:
        return len(self.unmatched)


def series_dir(root: Path, series: Series) -> Path:
    return root / (series.folder_name or series_folder(series.title))


def resolve_folders(root: Path, series: Series, extra_paths: list[str]) -> list[Path]:
    """All directories to scan for a series: the primary folder plus any extra
    folders. Extra paths may be relative to the root or absolute (pathlib joins
    an absolute right-hand side by replacing, so `root / abs` == abs)."""
    root = Path(root)
    values = [series.folder_name or series_folder(series.title), *extra_paths]
    folders: list[Path] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        p = root / value
        if str(p) not in seen:
            seen.add(str(p))
            folders.append(p)
    return folders


def resolve_volume_offsets(root: Path, series: Series) -> dict[str, int]:
    """Resolved shared-folder paths whose physical volumes need translating.

    ``None`` means an ordinary extra folder. An integer, including zero,
    scopes that folder to this series' tracked local volumes and translates
    physical volume ``N + offset`` to local volume ``N``.
    """
    root = Path(root)
    offsets: dict[str, int] = {}
    for folder in series.extra_folders:
        if folder.volume_offset is None:
            continue
        path = root / folder.path
        try:
            key = str(path.resolve(strict=False))
        except OSError:
            key = str(path.absolute())
        offsets[key] = folder.volume_offset
    return offsets


def _folder_match_titles(name: str) -> set[str]:
    """Normalized forms a folder name can match under: as-is, and with
    bracketed decorations stripped ("Berserk (1989)" also counts as
    "Berserk")."""
    forms = {normalize_title(name)}
    stripped = BRACKET_GROUPS.sub(" ", name)
    forms.add(normalize_title(stripped))
    forms.discard("")
    return forms


def _loose_folder_match(folder_norms: set[str], wanted: set[str]) -> bool:
    """Containment fallback for folders that add more than bracket junk —
    but only when the names are mostly the same text: a short title being a
    substring of a much longer folder name ("Monster" in "Monster Musume…")
    is a different series, not a naming variant."""
    for nn in folder_norms:
        for w in wanted:
            shorter, longer = sorted((nn, w), key=len)
            if shorter and shorter in longer and len(shorter) / len(longer) >= 0.6:
                return True
    return False


def find_existing_folder(root: Path, series: Series) -> str | None:
    """Return the sub-directory name of `root` whose normalized name matches
    the series title or an alt title, so mangarr can adopt a pre-existing
    folder even when it isn't named exactly like the series."""
    root = Path(root)
    if not root.is_dir():
        return None
    wanted = {normalize_title(series.title)}
    wanted.update(normalize_title(t) for t in series.alt_titles.split("\n") if t)
    wanted.discard("")
    best = None
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        folder_norms = _folder_match_titles(child.name)
        if folder_norms & wanted:
            return child.name  # exact normalized match wins immediately
        if best is None and _loose_folder_match(folder_norms, wanted):
            best = child.name
    return best


def _ownership_key(chapter: Chapter) -> tuple[str, int]:
    return ("db", chapter.id) if chapter.id is not None else ("memory", id(chapter))


def _title_only_volume(media: list[MediaFile], series: Series) -> list[MediaFile]:
    if series.total_volumes != 1:
        return media
    titles = {normalize_title(series.title)}
    titles.update(normalize_title(title) for title in series.alt_titles.split("\n") if title)
    return [
        replace(item, volume_number=1)
        if item.chapter_number is None
        and item.volume_number is None
        and not item.is_dir
        and normalize_title(item.path.stem) in titles
        else item
        for item in media
    ]


def _add_disk_chapters(
    series: Series, chapters: list[Chapter], media: list[MediaFile]
) -> int:
    """Create only chapter rows that disk evidence makes unambiguous.

    Decimal filenames explicitly identify specials that catalogue sources
    often omit. A title-named archive for a known one-volume series covers
    its provider-reported chapter count. Excluded tombstones in the full
    relationship always win and are never recreated.
    """
    known = {chapter.number for chapter in series.chapters}
    known.update(chapter.number for chapter in chapters)
    added = 0

    def add(chapter: Chapter) -> None:
        series.chapters.append(chapter)
        if chapters is not series.chapters:
            chapters.append(chapter)

    if not known and series.total_volumes == 1 and series.total_chapters:
        if any(item.volume_number == 1 and item.chapter_number is None for item in media):
            for number in range(1, series.total_chapters + 1):
                chapter = Chapter(
                    series_id=series.id, number=float(number), volume=1,
                    volume_source="disk-inferred", monitored=series.monitored,
                )
                add(chapter)
                known.add(chapter.number)
                added += 1

    by_number = {chapter.number: chapter for chapter in chapters}
    for item in media:
        number = item.chapter_number
        if number is None or number in known or float(number).is_integer():
            continue
        if not item.is_dir and comicinfo_number(item.path) in known:
            continue
        base = by_number.get(float(math.floor(number)))
        chapter = Chapter(
            series_id=series.id,
            number=number,
            volume=base.volume if base is not None else None,
            volume_source="disk-inferred" if base is not None and base.volume is not None else "",
            monitored=series.monitored,
        )
        add(chapter)
        by_number[number] = chapter
        known.add(number)
        added += 1
    return added


def scan_series(
    series: Series,
    chapters: list[Chapter],
    folders: list[Path],
    volume_offsets: dict[str, int] | None = None,
) -> ScanResult:
    """Mark chapters present across `folders` as downloaded (in place)."""
    folders = [Path(f) for f in folders]
    result = ScanResult()
    existing = [f for f in folders if f.exists()]
    if not existing:
        result.cleared = _reconcile(chapters, keep=set())
        return result

    media = find_series_media_files(existing, chapters, volume_offsets)
    media = _title_only_volume(media, series)
    result.added_chapters = _add_disk_chapters(series, chapters, media)
    match = match_files(media, chapters)
    scanned_paths = {str(item.path.resolve(strict=False)) for item in media}

    # Explicit page labels can disprove an old original-edition assignment
    # to an omnibus even though the recorded file still exists.
    known_content = {
        str(m.media.path.resolve()): m.content_chapters
        for m in match.matched if m.content_chapters is not None
    }
    for chapter in chapters:
        if not chapter.file_path:
            continue
        content = known_content.get(str(Path(chapter.file_path).resolve()))
        if content is not None and chapter.number not in content:
            if chapter.downloaded:
                result.cleared += 1
            chapter.downloaded = False
            chapter.file_path = ""

    owned_now: set[tuple[str, int]] = set()

    # exact chapter files first — they take precedence over volume coverage
    for mf in match.matched:
        if mf.chapter is None:
            continue
        chapter = mf.chapter
        # read ComicInfo lazily, only for files that matched a chapter and
        # only when the title could actually be applied (comicinfo_title
        # caches per file version, so repeat scans just stat)
        if not mf.media.is_dir and not getattr(chapter, "title_locked", False):
            title = comicinfo_title(mf.media.path)
            if title:
                apply_title(chapter, title, "comicinfo", series.title)
        path_str = str(mf.media.path)
        chapter_key = _ownership_key(chapter)
        if chapter_key not in owned_now and (
            not chapter.downloaded or chapter.file_path != path_str
        ):
            if not chapter.downloaded:
                result.matched_chapters += 1
            chapter.downloaded = True
            chapter.file_path = path_str
        owned_now.add(chapter_key)

    # whole-volume archives fill in any chapters not already covered exactly
    for mf in match.matched:
        if mf.chapter is not None:
            continue
        if mf.volume is not None:
            result.volume_files += 1
        path_str = str(mf.media.path)
        for chapter in mf.covered_chapters:
            chapter_key = _ownership_key(chapter)
            if chapter_key in owned_now:
                continue
            recorded_is_scanned = False
            if chapter.file_path:
                try:
                    recorded_is_scanned = str(
                        Path(chapter.file_path).resolve(strict=False)
                    ) in scanned_paths
                except OSError:
                    recorded_is_scanned = False
            if not chapter.downloaded or not chapter.file_path or not recorded_is_scanned:
                was_downloaded = chapter.downloaded
                chapter.downloaded = True
                chapter.file_path = path_str
                if not was_downloaded:
                    result.matched_chapters += 1
            owned_now.add(chapter_key)

    result.unmatched = match.unmatched
    result.cleared += _reconcile(chapters, keep=owned_now)
    log.info("Scanned %r across %d folder(s): +%d owned, +%d rows, %d volume files, "
             "%d unmatched, -%d cleared", series.title, len(existing),
             result.matched_chapters, result.added_chapters, result.volume_files,
             result.unmatched_count, result.cleared)
    return result


def _reconcile(chapters: list[Chapter], keep: set[tuple[str, int]]) -> int:
    """Clear downloaded state for chapters whose recorded file is gone."""
    cleared = 0
    for chapter in chapters:
        if not chapter.downloaded or _ownership_key(chapter) in keep:
            continue
        if not chapter.file_path or not Path(chapter.file_path).exists():
            chapter.downloaded = False
            chapter.file_path = ""
            cleared += 1
    return cleared
