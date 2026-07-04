"""Scan an existing library folder and adopt files in place.

Read-only with respect to the filesystem: it only records which tracked
chapters are already present on disk (setting Chapter.downloaded/file_path).
It never copies, moves, or writes files — that's what lets mangarr sit on top
of a library the user already has without re-downloading anything."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Chapter, Series
from ..util import normalize_title
from .matcher import MediaFile, find_media_files, match_files

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    matched_chapters: int = 0  # chapters newly marked owned
    volume_files: int = 0  # whole-volume archives found
    cleared: int = 0  # chapters whose recorded file vanished
    unmatched: list[MediaFile] = field(default_factory=list)

    @property
    def unmatched_count(self) -> int:
        return len(self.unmatched)


def series_dir(root: Path, series: Series) -> Path:
    from .naming import series_folder

    return root / (series.folder_name or series_folder(series.title))


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
        nn = normalize_title(child.name)
        if nn in wanted:
            return child.name  # exact normalized match wins immediately
        if best is None and nn and any(nn in w or w in nn for w in wanted):
            best = child.name
    return best


def scan_series(series: Series, chapters: list[Chapter], folder: Path) -> ScanResult:
    """Mark chapters present in `folder` as downloaded (in place)."""
    folder = Path(folder)
    result = ScanResult()
    if not folder.exists():
        # nothing on disk — reconcile away any stale ownership
        result.cleared = _reconcile(chapters, folder)
        return result

    match = match_files(find_media_files(folder), chapters)
    owned_now: set[int] = set()

    for mf in match.matched:
        path_str = str(mf.media.path)
        if mf.volume is not None and mf.chapter is None:
            result.volume_files += 1
        for chapter in mf.covered_chapters:
            if not chapter.downloaded or not chapter.file_path:
                chapter.downloaded = True
                chapter.file_path = path_str
                result.matched_chapters += 1
            owned_now.add(chapter.id)

    result.unmatched = match.unmatched
    result.cleared = _reconcile(chapters, folder, keep=owned_now)
    log.info("Scanned %r: +%d chapters, %d volume files, %d unmatched, -%d cleared",
             series.title, result.matched_chapters, result.volume_files,
             result.unmatched_count, result.cleared)
    return result


def _reconcile(chapters: list[Chapter], folder: Path, keep: set[int] | None = None) -> int:
    """Clear downloaded state for chapters whose recorded file is gone."""
    keep = keep or set()
    cleared = 0
    for chapter in chapters:
        if not chapter.downloaded or chapter.id in keep:
            continue
        if not chapter.file_path or not Path(chapter.file_path).exists():
            chapter.downloaded = False
            chapter.file_path = ""
            cleared += 1
    return cleared
