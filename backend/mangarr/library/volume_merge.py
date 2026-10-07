"""Pack a finished volume's chapter archives into one volume archive.

The chapters' pages are copied in reading order into a new CBZ (with a
volume-level ComicInfo.xml) that appears under its library name only once it
is complete. Deleting the chapter files is left to the caller, after the
database points at the new archive — a crash in between leaves duplicates
for the cleanup tool, never chapters without a file."""

import logging
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..download.cbz import build_comicinfo
from ..util import NEW_FILE_MODE, natural_key
from .matcher import IMAGE_EXTS

log = logging.getLogger(__name__)


def _page_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    pages = [
        info for info in archive.infolist()
        if not info.is_dir() and Path(info.filename).suffix.lower() in IMAGE_EXTS
    ]
    # mangarr's own writers zero-pad page names, but torrent-imported archives
    # often don't (1.jpg … 10.jpg): order numbers by value. Subfolders still
    # stay together, since the key compares the path prefix first.
    return sorted(pages, key=lambda info: natural_key(info.filename))


def merge_chapter_archives(
    chapter_files: list[tuple[float, Path]],
    dest: Path,
    series_title: str,
    volume: int,
    summary: str = "",
) -> int:
    """Write `dest` from the given (chapter number, .cbz path) pairs and
    return its page count. Refuses to overwrite an existing file and raises
    when any chapter archive is unreadable or has no pages."""
    if dest.exists():
        raise FileExistsError(f"volume archive already exists: {dest}")
    ordered = sorted(chapter_files, key=lambda item: item[0])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        dir=dest.parent, prefix=".mangarr-", suffix=".partial", delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        pages = 0
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as out:
            for index, (_number, path) in enumerate(ordered, start=1):
                with zipfile.ZipFile(path) as archive:
                    members = _page_members(archive)
                    if not members:
                        raise ValueError(f"no page images in {path}")
                    for page, info in enumerate(members, start=1):
                        ext = Path(info.filename).suffix.lower()
                        # chapter-then-page prefix keeps reading order under
                        # the name sort Kavita and Komga use
                        out.writestr(f"{index:04d}-{page:04d}{ext}", archive.read(info))
                        pages += 1
            out.writestr("ComicInfo.xml", build_comicinfo(
                series=series_title, volume=volume, summary=summary, page_count=pages,
            ))
        # not the temp file's 0600 — set once written, in case the umask
        # leaves no owner write
        temporary.chmod(NEW_FILE_MODE)
        temporary.replace(dest)
    finally:
        temporary.unlink(missing_ok=True)
    log.info("Merged %d chapter file(s) into %s (%d pages)", len(ordered), dest, pages)
    return pages
