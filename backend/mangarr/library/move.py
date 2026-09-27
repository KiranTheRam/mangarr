"""Moving a series to another root folder, files and all."""

import asyncio
import logging
import shutil
from pathlib import Path

from ..models import RootFolder, Series
from .scanner import series_dir

log = logging.getLogger(__name__)


class MoveError(Exception):
    """The series was left where it was; the message says why."""


def anchor_extra_folders(series: Series, old_root: Path) -> None:
    """Extra folders are stored relative to the root when they live under it,
    so a root change would silently point them into the new root. Pin them to
    where they actually are."""
    for folder in series.extra_folders:
        if not Path(folder.path).is_absolute():
            folder.path = str(old_root / folder.path)


def _rebase(path: str, old: Path, new: Path) -> str | None:
    try:
        return str(new / Path(path).relative_to(old))
    except ValueError:
        return None


async def move_series_folder(series: Series, new_root: RootFolder) -> bool:
    """Move the series' primary folder into `new_root` and repoint every
    chapter file and extra folder that lived inside it. Returns whether
    anything was moved (a series with no folder on disk yet has nothing to
    move). Never merges into or overwrites an existing folder, and raises
    MoveError before changing anything.

    The caller switches the root folder afterwards (see anchor_extra_folders)
    and must hold the series lock, so no refresh or import writes into the
    folder mid-move."""
    if series.root_folder is None:
        return False
    old_root = Path(series.root_folder.path)
    old_dir = series_dir(old_root, series)
    new_dir = series_dir(Path(new_root.path), series)
    if not old_dir.is_dir():
        return False
    if not Path(new_root.path).is_dir():
        raise MoveError(f"root folder {new_root.path} does not exist")
    if new_dir.exists():
        if new_dir.resolve() == old_dir.resolve():
            return False  # both roots reach the same directory
        raise MoveError(f"a folder named “{new_dir.name}” already exists in {new_root.path}")
    try:
        # a move across filesystems copies every file, so keep it off the loop
        await asyncio.to_thread(shutil.move, str(old_dir), str(new_dir))
    except OSError as exc:
        raise MoveError(f"could not move {old_dir}: {exc}") from exc
    for chapter in series.chapters:
        moved = _rebase(chapter.file_path, old_dir, new_dir) if chapter.file_path else None
        if moved is not None:
            # same file in a new place: its recorded origin still holds
            source, group = chapter.file_source, chapter.file_group
            chapter.file_path = moved
            chapter.file_source, chapter.file_group = source, group
    for folder in series.extra_folders:
        # a relative path resolves against the root (an absolute one as-is)
        moved = _rebase(str(old_root / folder.path), old_dir, new_dir)
        if moved is not None:
            folder.path = moved
    log.info("Moved %r from %s to %s", series.title, old_dir, new_dir)
    return True
