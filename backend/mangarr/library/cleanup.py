"""Find and remove duplicate / orphaned files in a series' folders.

Compare chapter coverage as well as file identity: a volume can overlap
several chapter files even when every file is referenced. Defaults preserve
at least one copy of each covered chapter. A file whose name can't prove what
it holds is never offered and never stands in as another file's copy. Apply
rechecks the entire deletion batch against files still on disk and repoints
chapters only after deletion.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Chapter, Series
from ..util import BRACKET_GROUPS, CHAPTER_PREFIX_PATTERN, TRAILING_NUMBER_PATTERN, VOLUME_PATTERN
from .matcher import MediaFile, find_media_files, match_files
from .naming import chapter_filename, series_folder, volume_filename

log = logging.getLogger(__name__)


@dataclass
class CleanupFile:
    path: str
    size: int
    referenced: bool  # a downloaded chapter points at this file
    keep: bool  # recommended default

    @property
    def name(self) -> str:
        return Path(self.path).name


@dataclass
class CleanupGroup:
    label: str  # "Volume 3" / "Chapter 12"
    files: list[CleanupFile]


@dataclass
class CleanupPlan:
    groups: list[CleanupGroup] = field(default_factory=list)  # >1 file for one thing
    orphans: list[CleanupFile] = field(default_factory=list)  # standalone extras
    overlaps: list[CleanupFile] = field(default_factory=list)  # referenced but redundant


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _identity(mf: MediaFile, tracked_ch: set[float], tracked_vol: set[int]):
    if mf.chapter_number is not None and mf.chapter_number in tracked_ch:
        return ("ch", mf.chapter_number)
    if mf.volume_number is not None and mf.volume_number in tracked_vol:
        return ("vol", mf.volume_number)
    return ("unknown", str(mf.path))


# "Ch. 12-15", "c001 - c010", "v01-03": a marked span of chapters/volumes
_MARKED_SPAN = re.compile(
    r"(?<![a-z])(?:c(?:h(?:apter)?)?|v(?:ol(?:ume)?)?)[ ._-]{0,2}\d+(?:\.\d+)?\s*[-–—]\s*"
    r"(?:(?:c(?:h(?:apter)?)?|v(?:ol(?:ume)?)?)[ ._-]{0,2})?\d",
    re.IGNORECASE,
)
# "Kagurabachi 001-010": a bare span at the end of the untagged name
_BARE_SPAN = re.compile(r"\b\d+(?:\.\d+)?\s*[-–—]\s*\d+(?:\.\d+)?\s*$")


def _chapter_markers(text: str) -> set[float]:
    return {float(n.lower().replace("x", ".")) for n in CHAPTER_PREFIX_PATTERN.findall(text)}


def _provable(mf: MediaFile) -> bool:
    """Whether a file's name pins down what it holds well enough to offer it
    (or a file it seems to duplicate) for deletion. The filename parser takes
    the first number it finds, so it can't be trusted when the name:
      - spans chapters or volumes ("Ch. 12-15", "v01-03", "001-010");
      - is read as a chapter but also has a volume marker ("v01 (c1fi7)");
      - has chapter numbers that disagree, including one only inside a tag
        ("(c1fi7)") or a title number ("C3 - Cube x Cursed x Curious 012").
    Such files are kept by default and never prove another file redundant;
    the user can still select them."""
    stem = mf.path.name if mf.is_dir else mf.path.stem
    untagged = BRACKET_GROUPS.sub(" ", stem).strip()
    if _MARKED_SPAN.search(stem) or _BARE_SPAN.search(untagged):
        return False
    if mf.chapter_number is None:
        return True  # volume archives and unknown files keep their rules
    if VOLUME_PATTERN.search(stem):
        return False
    markers = _chapter_markers(untagged)
    if len(_chapter_markers(stem)) > 1 or _chapter_markers(stem) != markers:
        return False
    trailing = TRAILING_NUMBER_PATTERN.search(untagged)
    return not (markers and trailing and float(trailing.group(1)) not in markers)


def _all_media(folders: list[Path]) -> list[MediaFile]:
    media: dict[str, MediaFile] = {}
    for folder in folders:
        if Path(folder).exists():
            for mf in find_media_files(folder):
                media.setdefault(_canonical(mf.path), mf)
    return list(media.values())


def _canonical(path: str | Path) -> str:
    try:
        return str(Path(path).resolve(strict=False))
    except OSError:
        return str(Path(path).absolute())


def _coverage(media: list[MediaFile], chapters: list[Chapter]) -> dict[str, set[float]]:
    """Content labels, filename matches and otherwise unknown manual ranges.

    An old pointer cannot prove coverage that explicit archive labels disprove.
    """
    coverage = {_canonical(mf.path): set() for mf in media}
    known_content: set[str] = set()
    for match in match_files(media, chapters).matched:
        key = _canonical(match.media.path)
        coverage[key].update(
            match.content_chapters if match.content_chapters is not None
            else (c.number for c in match.covered_chapters)
        )
        if match.content_chapters is not None:
            known_content.add(key)
    for chapter in chapters:
        if chapter.file_path:
            key = _canonical(chapter.file_path)
            if key in coverage and key not in known_content:
                coverage[key].add(chapter.number)
    return coverage


def _requirements(coverage: dict[str, set[float]], chapters: list[Chapter]) -> dict[str, set[float]]:
    """Deletion must also repair any old references to the removed file."""
    required = {key: set(numbers) for key, numbers in coverage.items()}
    for chapter in chapters:
        if chapter.file_path:
            key = _canonical(chapter.file_path)
            if key in required:
                required[key].add(chapter.number)
    return required


def _covered_elsewhere(
    key: str, coverage: dict[str, set[float]], retained: set[str], required: set[float]
) -> bool:
    return bool(required) and required <= set().union(
        *(coverage[path] for path in retained if path != key)
    )


def analyze(
    series: Series,
    chapters: list[Chapter],
    folders: list[Path],
    template: str,
    template_no_volume: str,
    media: list[MediaFile] | None = None,
) -> CleanupPlan:
    media = list(media) if media is not None else _all_media(folders)
    referenced = {_canonical(c.file_path) for c in chapters if c.file_path}
    tracked_ch = {c.number for c in chapters}
    tracked_vol = {c.volume for c in chapters if c.volume is not None}
    ch_by_num = {c.number: c for c in chapters}
    coverage = _coverage(media, chapters)
    required = _requirements(coverage, chapters)
    unprovable = {_canonical(mf.path) for mf in media if not _provable(mf)}
    retained = set(coverage) - unprovable

    by_identity: dict[tuple, list[MediaFile]] = {}
    for mf in media:
        # Radio groups are interchangeable copies, not partial overlaps. A
        # manual mapping may give similarly named files different coverage.
        # Loose-image directories and unprovable names remain keep-only.
        canon = _canonical(mf.path)
        if not mf.is_dir and canon not in unprovable and required[canon] == coverage[canon]:
            identity = _identity(mf, tracked_ch, tracked_vol)
            key = (*identity, frozenset(coverage[_canonical(mf.path)]))
            by_identity.setdefault(key, []).append(mf)

    plan = CleanupPlan()
    grouped: set[str] = set()
    for identity, files in by_identity.items():
        kind, num, covered = identity
        if len(files) > 1 and covered and kind != "unknown":
            group = _group(series, kind, num, files, referenced,
                           ch_by_num, template, template_no_volume)
            plan.groups.append(group)
            grouped.update(_canonical(f.path) for f in group.files)
            retained.difference_update(_canonical(f.path) for f in group.files if not f.keep)

    # Drop unreferenced extras first, then narrow chapter files. Reevaluate
    # against the survivors of earlier recommendations so a chapter and the
    # volume covering it can never both be selected for deletion by default.
    candidates = [mf for mf in media if _canonical(mf.path) not in grouped]
    candidates.sort(key=lambda mf: (
        _canonical(mf.path) in referenced,
        len(coverage[_canonical(mf.path)]),
        str(mf.path),
    ))
    for mf in candidates:
        path = str(mf.path)
        key = _canonical(path)
        in_use = key in referenced
        redundant = (not mf.is_dir and key not in unprovable
                     and _covered_elsewhere(key, coverage, retained, required[key]))
        if redundant:
            retained.remove(key)
        file = CleanupFile(path, _size(path), in_use, keep=not redundant)
        if in_use:
            if redundant:
                plan.overlaps.append(file)
        else:
            plan.orphans.append(file)

    plan.groups.sort(key=lambda g: g.label)
    plan.orphans.sort(key=lambda f: f.path)
    plan.overlaps.sort(key=lambda f: f.path)
    return plan


def _group(series, kind, num, files, referenced, ch_by_num, template, template_no_volume):
    ext = Path(str(files[0].path)).suffix.lower()
    if kind == "ch":
        ch = ch_by_num[num]
        canonical = chapter_filename(template, template_no_volume, series.title,
                                     ch.number, ch.volume, ch.title, ext=ext)
        label = f"Chapter {num:g}"
    else:
        canonical = volume_filename(series_folder(series.title), num, ext)
        label = f"Volume {num}"

    cfiles = [CleanupFile(str(mf.path), _size(str(mf.path)), _canonical(mf.path) in referenced, False)
              for mf in files]
    # default to keeping the file that's already in use (the established library
    # copy), so cleanup only removes the accidental duplicate and never has to
    # delete an in-use file. Fall back to the canonically-named one, then size.
    keeper = next((c for c in cfiles if c.referenced), None)
    if keeper is None:
        keeper = next((c for c in cfiles if Path(c.path).name == canonical), None)
    if keeper is None:
        keeper = max(cfiles, key=lambda c: c.size)
    keeper.keep = True
    return CleanupGroup(label, cfiles)


@dataclass
class CleanupResult:
    deleted: int = 0
    repointed: int = 0
    skipped: int = 0
    freed_bytes: int = 0


def apply_cleanup(
    series: Series,
    chapters: list[Chapter],
    folders: list[Path],
    delete_paths: list[str],
    media: list[MediaFile] | None = None,
) -> CleanupResult:
    media = list(media) if media is not None else _all_media(folders)
    media_by_path = {_canonical(mf.path): mf for mf in media}
    coverage = _coverage(media, chapters)
    required = _requirements(coverage, chapters)
    delete_set = {_canonical(path) for path in delete_paths}
    result = CleanupResult()
    processed: set[str] = set()

    for raw_path in delete_paths:
        key = _canonical(raw_path)
        if key in processed:
            continue
        processed.add(key)
        mf = media_by_path.get(key)
        if mf is None:
            log.warning("cleanup: refusing path outside series media: %s", raw_path)
            result.skipped += 1
            continue
        path = str(mf.path)
        if not os.path.exists(path):
            continue
        if mf.is_dir:
            result.skipped += 1
            continue
        survivors = {
            other for other, item in media_by_path.items()
            if other not in delete_set and item.path.exists() and _provable(item)
        }
        # Protect all known coverage, even if this file is unreferenced or the
        # database's downloaded flag is stale. Missing files prove nothing, and
        # neither do names _provable rejects.
        if required[key] and not _covered_elsewhere(key, coverage, survivors, required[key]):
            result.skipped += 1
            continue
        referencing = [c for c in chapters if c.file_path and _canonical(c.file_path) == key]
        replacements = {
            c.number: str(media_by_path[next(
                other for other in sorted(survivors) if c.number in coverage[other]
            )].path)
            for c in referencing
        }
        size = _size(path)
        try:
            os.remove(path)
        except OSError as exc:
            log.warning("cleanup: could not delete %s: %s", path, exc)
            result.skipped += 1
            continue
        for c in referencing:
            c.file_path = replacements[c.number]
        result.repointed += len(referencing)
        result.deleted += 1
        result.freed_bytes += size
        log.info("cleanup: deleted %s", path)
    return result
