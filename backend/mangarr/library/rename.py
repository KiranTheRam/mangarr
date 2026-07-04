"""Preview and apply renames of owned files into mangarr's naming convention.

Format-preserving: a .cbr is renamed, never converted. Non-destructive preview;
apply moves files within the library, skips target collisions, never deletes."""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..models import Chapter, Series
from .naming import chapter_filename, series_folder, volume_filename

log = logging.getLogger(__name__)


@dataclass
class RenameItem:
    chapter_ids: list[int]  # chapters that point at this file (>1 for volumes)
    current_path: str
    new_path: str

    @property
    def current_name(self) -> str:
        return Path(self.current_path).name

    @property
    def new_name(self) -> str:
        return Path(self.new_path).name


def _desired_name(series: Series, chapter: Chapter, ext: str,
                  template: str, template_no_volume: str) -> str:
    base = chapter_filename(
        template, template_no_volume, series.title,
        chapter.number, chapter.volume, chapter.title, ext=ext,
    )
    return base


def plan_renames(
    series: Series,
    chapters: list[Chapter],
    template: str,
    template_no_volume: str,
) -> list[RenameItem]:
    """Rename items for owned chapters whose on-disk name differs from the
    naming convention. Each file is renamed in place (within its own
    directory), so a series that spans a volumes folder and a chapters folder
    keeps that split. Files shared by several chapters (whole-volume archives)
    produce one item, named with the volume convention; single chapter files
    use the chapter convention."""
    # group chapters by the file they point at
    by_file: dict[str, list[Chapter]] = {}
    for ch in chapters:
        if ch.downloaded and ch.file_path:
            by_file.setdefault(ch.file_path, []).append(ch)

    items: list[RenameItem] = []
    for current, chs in by_file.items():
        current_path = Path(current)
        ext = current_path.suffix.lower()
        volumes = {c.volume for c in chs}
        if len(chs) > 1 and len(volumes) == 1 and None not in volumes:
            # one archive covering a whole volume → volume naming
            desired = volume_filename(series_folder(series.title), chs[0].volume, ext)
        elif len(chs) == 1:
            # a single chapter file → chapter naming
            desired = _desired_name(series, chs[0], ext, template, template_no_volume)
        else:
            # ambiguous grouping (multiple chapters, mixed volumes) — leave it
            continue
        # rename in place, in the file's own directory
        new_path = current_path.parent / desired
        if new_path.name != current_path.name:
            items.append(RenameItem(
                chapter_ids=[c.id for c in chs],
                current_path=str(current_path),
                new_path=str(new_path),
            ))
    items.sort(key=lambda i: i.current_name)
    return items


@dataclass
class RenameOutcome:
    item: RenameItem
    status: str  # "renamed" | "skipped-missing" | "skipped-collision" | "error"
    detail: str = ""


def apply_renames(items: list[RenameItem], chapter_by_id: dict[int, Chapter]) -> list[RenameOutcome]:
    """Move each file to its new path and update the chapters that reference it.
    Never overwrites an existing target and never deletes the source."""
    outcomes: list[RenameOutcome] = []
    for item in items:
        src = Path(item.current_path)
        dst = Path(item.new_path)
        if not src.exists():
            outcomes.append(RenameOutcome(item, "skipped-missing"))
            continue
        if dst.exists() and dst != src:
            outcomes.append(RenameOutcome(item, "skipped-collision", str(dst)))
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except OSError as exc:
            outcomes.append(RenameOutcome(item, "error", str(exc)))
            continue
        for cid in item.chapter_ids:
            ch = chapter_by_id.get(cid)
            if ch is not None:
                ch.file_path = str(dst)
        log.info("Renamed %s -> %s", src.name, dst.name)
        outcomes.append(RenameOutcome(item, "renamed"))
    return outcomes
