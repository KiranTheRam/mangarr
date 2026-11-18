"""Identify library files inside ZIP archives without reading their payloads."""

from collections.abc import Iterable, Iterator
from pathlib import PurePosixPath
from zipfile import ZipInfo

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
# Plenty for ordinary metadata, independent of the archive's compressed size.
MAX_COMICINFO_BYTES = 1024 * 1024

# These system, NAS, sync, and reader paths are also excluded by Kavita.
_EXCLUDED_COMPONENTS = {
    "#recycle", "#snapshot", "$recycle.bin", ".@__thumb", ".appledouble",
    ".caltrash", ".documentrevisions-v100", ".fseventsd", ".qpkg", ".recycle",
    ".snapshots", ".spotlight-v100", ".stfolder", ".stversions",
    ".temporaryitems", ".trash", ".trashes", ".wdmc", ".yacreaderlibrary",
    ".zfs", "@eadir", "@recently-snapshot", "@recycle", "__macosx",
    "lost+found", "system volume information",
}

ArchiveFile = tuple[ZipInfo, PurePosixPath]


def archive_files(entries: Iterable[ZipInfo]) -> Iterator[ArchiveFile]:
    """Yield archive files other than operating-system metadata and snapshots."""
    for entry in entries:
        if entry.filename.endswith(("/", "\\")):
            continue
        path = PurePosixPath(entry.filename.replace("\\", "/"))
        if any(
            part.casefold() in _EXCLUDED_COMPONENTS or part.casefold().startswith(".trash-")
            or part.startswith("._")
            for part in path.parts
        ):
            continue
        yield entry, path


def archive_pages(files: Iterable[ArchiveFile]) -> Iterator[ArchiveFile]:
    # Readers skip dot-prefixed ZIP names when counting pages. Use the raw
    # name because PurePosixPath removes a leading "./".
    return (
        (entry, path) for entry, path in files
        if not entry.filename.startswith(".") and path.suffix.lower() in IMAGE_EXTS
    )


def find_comicinfo_member(files: Iterable[ArchiveFile]) -> ZipInfo | None:
    """Select a single ComicInfo entry, including uniquely named nested metadata."""
    matches = (entry for entry, path in files if path.name.casefold() == "comicinfo.xml")
    member = next(matches, None)
    if next(matches, None) is not None:
        raise ValueError("Archive contains multiple ComicInfo.xml files")
    return member
