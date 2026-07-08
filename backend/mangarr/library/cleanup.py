"""Find and remove duplicate / orphaned files in a series' folders.

Common after grabbing a volume torrent onto a library that already has the
same volumes: two files end up representing the same volume/chapter. This
groups files by what they represent, recommends which copy to keep (the one
matching mangarr's naming, else the referenced one, else the largest), and —
on apply — deletes the rest and re-points any chapters at the survivor. It
never deletes the last copy backing a downloaded chapter."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Chapter, Series
from .matcher import MediaFile, find_media_files
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


def _all_media(folders: list[Path]) -> list[MediaFile]:
    media: list[MediaFile] = []
    for folder in folders:
        if Path(folder).exists():
            media.extend(find_media_files(folder))
    return media


def _canonical(path: str | Path) -> str:
    try:
        return str(Path(path).resolve(strict=False))
    except OSError:
        return str(Path(path).absolute())


def analyze(
    series: Series,
    chapters: list[Chapter],
    folders: list[Path],
    template: str,
    template_no_volume: str,
) -> CleanupPlan:
    media = _all_media(folders)
    referenced = {c.file_path for c in chapters if c.downloaded and c.file_path}
    tracked_ch = {c.number for c in chapters}
    tracked_vol = {c.volume for c in chapters if c.volume is not None}
    ch_by_num = {c.number: c for c in chapters}
    vol_downloaded = {
        v: all(c.downloaded for c in chapters if c.volume == v) for v in tracked_vol
    }

    by_identity: dict[tuple, list[MediaFile]] = {}
    for mf in media:
        by_identity.setdefault(_identity(mf, tracked_ch, tracked_vol), []).append(mf)

    plan = CleanupPlan()
    for identity, files in by_identity.items():
        kind, num = identity
        if len(files) > 1:
            plan.groups.append(_group(series, kind, num, files, referenced,
                                      ch_by_num, template, template_no_volume))
            continue
        mf = files[0]
        path = str(mf.path)
        if path in referenced:
            continue  # single, in use — nothing to clean
        # a standalone unreferenced file: redundant if its content is already
        # covered elsewhere (a downloaded chapter / a fully-downloaded volume)
        if kind == "ch":
            redundant = num in tracked_ch and ch_by_num[num].downloaded
        elif kind == "vol":
            redundant = vol_downloaded.get(num, False)
        else:
            redundant = False  # unknown extra — keep by default
        plan.orphans.append(CleanupFile(path, _size(path), False, keep=not redundant))

    plan.groups.sort(key=lambda g: g.label)
    plan.orphans.sort(key=lambda f: f.path)
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

    cfiles = [CleanupFile(str(mf.path), _size(str(mf.path)), str(mf.path) in referenced, False)
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
    series: Series, chapters: list[Chapter], folders: list[Path], delete_paths: list[str]
) -> CleanupResult:
    media = _all_media(folders)
    tracked_ch = {c.number for c in chapters}
    tracked_vol = {c.volume for c in chapters if c.volume is not None}
    media_by_path = {_canonical(mf.path): mf for mf in media}
    identity_of = {_canonical(mf.path): _identity(mf, tracked_ch, tracked_vol) for mf in media}
    delete_set = {_canonical(path) for path in delete_paths}
    result = CleanupResult()

    for raw_path in delete_paths:
        key = _canonical(raw_path)
        mf = media_by_path.get(key)
        if mf is None:
            log.warning("cleanup: refusing path outside series media: %s", raw_path)
            result.skipped += 1
            continue
        path = str(mf.path)
        if not os.path.exists(path):
            continue
        referencing = [c for c in chapters if c.file_path and _canonical(c.file_path) == key]
        if referencing:
            ident = identity_of.get(key)
            survivors = [
                str(mf.path) for mf in media
                if identity_of.get(_canonical(mf.path)) == ident
                and _canonical(mf.path) not in delete_set
                and os.path.exists(str(mf.path))
            ]
            if not survivors:
                result.skipped += 1  # would leave a chapter with no file — refuse
                continue
            for c in referencing:
                c.file_path = survivors[0]
            result.repointed += len(referencing)
        size = _size(path)
        try:
            os.remove(path)
        except OSError as exc:
            log.warning("cleanup: could not delete %s: %s", path, exc)
            result.skipped += 1
            continue
        result.deleted += 1
        result.freed_bytes += size
        log.info("cleanup: deleted %s", path)
    return result
