"""Pack a finished volume's chapter archives into one volume archive.

The chapters' pages are copied in reading order into a new CBZ (with a
volume-level ComicInfo.xml) that appears under its library name only once it
is complete. Deleting the chapter files is left to the caller, after the
database points at the new archive — a crash in between leaves duplicates
for the cleanup tool, never chapters without a file."""

import json
import logging
import zipfile
from io import BufferedReader
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..download.cbz import build_comicinfo
from ..util import NEW_FILE_MODE, natural_key
from .matcher import IMAGE_EXTS, PAGE_PROVENANCE_NAME, read_archive_source_page_signature

log = logging.getLogger(__name__)

MAX_MERGE_PAGES = 10_000
MAX_MERGE_PAGE_BYTES = 32 * 1024 * 1024
MAX_MERGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ZIP_DIRECTORY_BYTES = 4 * 1024 * 1024
MAX_ZIP_ENTRIES = 12_000
COPY_CHUNK_BYTES = 64 * 1024


class _MergeArchiveReader(BufferedReader):
    def read(self, size: int = -1) -> bytes:
        # ZipFile loads the central directory in one read, before it exposes
        # entry counts. Bound that allocation as well as streamed page reads.
        if size > MAX_ZIP_DIRECTORY_BYTES:
            raise ValueError("chapter ZIP directory exceeds the size limit")
        data = super().read(MAX_ZIP_DIRECTORY_BYTES + 1 if size < 0 else size)
        if len(data) > MAX_ZIP_DIRECTORY_BYTES:
            raise ValueError("chapter ZIP directory exceeds the size limit")
        return data


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
        temporary.chmod(0o600)  # writable whatever the umask; final mode below
        pages = 0
        expanded_bytes = 0
        original_signature = []
        archived_signature = []
        provenance_valid = True
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as out:
            for index, (_number, path) in enumerate(ordered, start=1):
                with _MergeArchiveReader(path.open("rb", buffering=0)) as handle, zipfile.ZipFile(handle) as archive:
                    if len(archive.infolist()) > MAX_ZIP_ENTRIES:
                        raise ValueError("chapter ZIP exceeds the entry count limit")
                    members = _page_members(archive)
                    if not members:
                        raise ValueError(f"no page images in {path}")
                    if pages + len(members) > MAX_MERGE_PAGES:
                        raise ValueError("volume merge exceeds the page count limit")
                    source_members = [info for info in archive.infolist()
                                      if not info.is_dir() and Path(info.filename).suffix.lower() in IMAGE_EXTS]
                    stored = tuple((info.CRC, info.file_size) for info in source_members)
                    source_signature = read_archive_source_page_signature(archive, stored)
                    # Provenance follows ZIP entry order; the merged output
                    # uses natural filename order, so remap by entry identity.
                    by_member = dict(zip(map(id, source_members), source_signature or ()))
                    provenance_valid = provenance_valid and source_signature is not None
                    for page, info in enumerate(members, start=1):
                        # BZIP2 and LZMA can expand a large block inside
                        # ZipExtFile before returning a bounded read.
                        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                            raise ValueError(f"unsupported page compression in {path}")
                        if info.file_size > MAX_MERGE_PAGE_BYTES or expanded_bytes + info.file_size > MAX_MERGE_BYTES:
                            raise ValueError("volume merge exceeds the expanded size limit")
                        if source_signature is not None:
                            original_signature.append(by_member[id(info)])
                        archived_signature.append((info.CRC, info.file_size))
                        ext = Path(info.filename).suffix.lower()
                        # chapter-then-page prefix keeps reading order under
                        # the name sort Kavita and Komga use
                        name = f"{index:04d}-{page:04d}{ext}"
                        with archive.open(info) as source, out.open(name, "w") as target:
                            remaining = info.file_size
                            while chunk := source.read(COPY_CHUNK_BYTES):
                                remaining -= len(chunk)
                                if remaining < 0:
                                    raise ValueError("volume page exceeds its declared size")
                                target.write(chunk)
                            if remaining:
                                raise ValueError("volume page is shorter than its declared size")
                        expanded_bytes += info.file_size
                        pages += 1
            if provenance_valid and original_signature != archived_signature:
                out.writestr(PAGE_PROVENANCE_NAME, json.dumps({
                    "version": 1, "original": original_signature, "archived": archived_signature,
                }, separators=(",", ":")))
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
