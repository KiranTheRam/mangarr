"""Shared, read-only matching of on-disk files to tracked chapters.

Used by the importer, scanner and cleanup. Volume CBZ/ZIP archives with explicit
chapter labels on every page use those labels instead of assuming the release's
volume number matches the metadata edition. Archive directories and ComicInfo
titles are cached per file version; page images are never read or written."""

import json
import os
import re
import stat
import threading
import zipfile
import zlib
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar
from xml.etree import ElementTree

from ..filename import BRACKET_GROUPS, FilenameCoverage, parse_filename
from ..models import Chapter
from ..util import has_chapter_marker, parse_chapter_number
from .archive_members import (
    IMAGE_EXTS, MAX_COMICINFO_BYTES, ArchiveVersion, archive_files, archive_pages, archive_version,
    find_comicinfo_member,
)

T = TypeVar("T")

ARCHIVE_EXTS = {".cbz", ".zip", ".cbr", ".rar", ".cb7", ".7z"}
SIDECAR_IMAGE_STEMS = {
    "banner", "cover", "fanart", "folder", "poster", "thumb", "thumbnail",
}
_PDF_HEADER = re.compile(rb"%PDF-\d\.\d")


@dataclass
class MediaFile:
    path: Path
    is_dir: bool  # a directory of loose images (one chapter/volume as pages)
    chapter_number: float | None
    volume_number: int | None
    coverage: FilenameCoverage | None = None

    @property
    def label(self) -> str:
        return self.path.name

    @property
    def is_pdf(self) -> bool:
        return not self.is_dir and self.path.suffix.lower() == ".pdf"


@dataclass
class MatchedFile:
    media: MediaFile
    chapter: Chapter | None  # the single chapter this file is, if any
    volume: int | None  # set when the file is a whole-volume archive
    covered_chapters: list[Chapter] = field(default_factory=list)
    content_chapters: frozenset[float] | None = None  # explicit page labels or chapter spans


@dataclass
class MatchResult:
    matched: list[MatchedFile]
    unmatched: list[MediaFile]


def _name_source(path: Path, is_dir: bool) -> str:
    """The text we parse the chapter/volume number from."""
    return path.name if is_dir else path.stem


def pdf_header_present(path: Path) -> bool | None:
    """Check a PDF candidate's header. Unreadable files give no evidence."""
    if path.suffix.lower() != ".pdf":
        return None
    if path.name.startswith("._"):
        return False  # macOS AppleDouble sidecar
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                return _PDF_HEADER.search(stream.read(1024)) is not None
    except OSError:
        pass
    return None


def find_media_files(
    content_path: Path, files: Iterable[Path] | None = None, *, include_pdfs: bool = True,
) -> list[MediaFile]:
    """Archives, existing PDFs and directories that directly hold loose images.
    Given `files`, only those are considered instead of walking content_path."""
    content_path = Path(content_path)
    media: list[MediaFile] = []

    if files is None:
        if content_path.is_file():
            if content_path.suffix.lower() in ARCHIVE_EXTS or (
                include_pdfs and pdf_header_present(content_path)
            ):
                media.append(_media_of(content_path, is_dir=False))
            return media

        if not content_path.is_dir():
            return media
        files = content_path.rglob("*")

    image_dirs: set[Path] = set()
    for p in sorted(files):
        if not p.is_file():
            continue
        suffix = p.suffix.lower()
        if suffix in ARCHIVE_EXTS or (include_pdfs and pdf_header_present(p)):
            media.append(_media_of(p, is_dir=False))
        elif suffix in IMAGE_EXTS and p.stem.lower() not in SIDECAR_IMAGE_STEMS:
            image_dirs.add(p.parent)

    for d in sorted(image_dirs):
        media.append(_media_of(d, is_dir=True))
    return media


def _media_of(path: Path, is_dir: bool) -> MediaFile:
    text = _name_source(path, is_dir)
    parsed = parse_filename(text)
    return MediaFile(path, is_dir, parsed.chapter_number, parsed.volume_number, parsed)


def _pdf_title_key(title: str) -> str:
    # Keep native-script titles distinct, even when they contain ASCII digits.
    return re.sub(r"[\W_]+", " ", title.casefold().replace("×", "x")).strip()


def filename_matches_title(coverage: FilenameCoverage, title: str) -> bool:
    key = _pdf_title_key(title)
    return bool(key and any(_pdf_title_key(candidate) == key for candidate in coverage.title_candidates))


def pdf_file_identity(path: Path) -> tuple[int, int] | None:
    try:
        file_stat = path.stat()
    except OSError:
        return None
    if stat.S_ISREG(file_stat.st_mode):
        return file_stat.st_dev, file_stat.st_ino
    return None


def manual_pdf_mappings(chapters: Iterable[Chapter]) -> dict[tuple[int, int], list[Chapter]]:
    """Find explicit single-chapter PDF mappings across aliases and hardlinks."""
    mappings: dict[tuple[int, int], list[Chapter]] = {}
    for chapter in chapters:
        if not chapter.downloaded or chapter.pdf_mapping != "manual" or not chapter.file_path:
            continue
        if key := pdf_file_identity(Path(chapter.file_path)):
            mappings.setdefault(key, []).append(chapter)
    return mappings


def split_pdf_title_conflicts(
    media: list[MediaFile], titles: Iterable[str], *, manual_pdfs: Collection[tuple[int, int]] = (),
) -> tuple[list[MediaFile], list[MediaFile]]:
    """Keep titled PDFs for other series available for explicit mapping."""
    title_keys = {_pdf_title_key(form) for name in titles
                  for form in (name, BRACKET_GROUPS.sub(" ", name))}
    title_keys.discard("")
    eligible, conflicts = [], []
    for item in media:
        candidates = item.coverage.title_candidates if item.is_pdf and item.coverage else ()
        if (candidates and all(_pdf_title_key(title) not in title_keys for title in candidates)
                and (not manual_pdfs or pdf_file_identity(item.path) not in manual_pdfs)):
            conflicts.append(item)
        else:
            eligible.append(item)
    return eligible, conflicts


_ARCHIVE_READ_ERRORS = (
    zipfile.BadZipFile, ValueError, RuntimeError, EOFError, LookupError,
    zlib.error, ElementTree.ParseError,
)


class _ArchiveCache:
    """Values read from the current version of each archive path.

    Opening every archive on every scan is far too slow for a network-mounted
    library, so each file version is read only once. The value is read through
    the descriptor whose fstat forms the version, so a replacement that keeps
    the size and mtime, or one racing the read, cannot reuse another inode's
    value. Transient I/O errors are not cached.
    """

    def __init__(self, maxsize: int | None = None):
        self._maxsize = maxsize
        self._items: OrderedDict[str, tuple[ArchiveVersion, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, path: Path, read: Callable[[zipfile.ZipFile], T], default: T) -> T:
        if path.suffix.lower() not in {".cbz", ".zip"}:
            return default
        key = str(path)
        try:
            # A hit only needs a stat, which matters on network mounts. A miss
            # reads through the descriptor whose fstat forms the cached version.
            current = path.stat()
        except OSError:
            return default
        if stat.S_ISREG(current.st_mode):
            with self._lock:
                cached = self._items.get(key)
                if cached is not None and cached[0] == archive_version(current):
                    self._items.move_to_end(key)
                    return cached[1]
        try:
            # O_NONBLOCK keeps a FIFO with an archive suffix from hanging a scan.
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return default
        with os.fdopen(fd, "rb") as handle:
            try:
                file_stat = os.fstat(fd)
            except OSError:
                return default
            if not stat.S_ISREG(file_stat.st_mode):
                return default
            version = archive_version(file_stat)
            with self._lock:
                cached = self._items.get(key)
                if cached is not None and cached[0] == version:
                    self._items.move_to_end(key)
                    return cached[1]
            try:
                with zipfile.ZipFile(handle) as archive:
                    value = read(archive)
            except OSError:
                return default
            except _ARCHIVE_READ_ERRORS:
                value = default
        with self._lock:
            self._items[key] = (version, value)
            self._items.move_to_end(key)
            if self._maxsize is not None and len(self._items) > self._maxsize:
                self._items.popitem(last=False)
        return value


_comicinfo_title_cache = _ArchiveCache()
_comicinfo_number_cache = _ArchiveCache()
_chapter_numbers_cache = _ArchiveCache(256)
_page_signature_cache = _ArchiveCache(1024)
_source_page_signature_cache = _ArchiveCache(1024)


def comicinfo_title(path: Path) -> str:
    """The standard ComicInfo Title field from a CBZ/ZIP, if present."""
    return _comicinfo_title_cache.get(path, _read_comicinfo_title, "")


def _read_comicinfo(archive: zipfile.ZipFile) -> ElementTree.Element | None:
    member = find_comicinfo_member(archive_files(archive.infolist()))
    if (member is None or member.file_size > MAX_COMICINFO_BYTES
            or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
        return None
    # zipfile's BZIP2/LZMA readers can allocate unbounded decompressed
    # output before applying read(n), unlike STORED/DEFLATED. Ignore
    # metadata using those methods, even if the declared size is small.
    # Pass the actual ZipInfo (duplicate names must not change which
    # entry was checked), and bound decompression even for bad headers.
    with archive.open(member) as stream:
        data = stream.read(MAX_COMICINFO_BYTES + 1)
    if len(data) > MAX_COMICINFO_BYTES or len(data) != member.file_size:
        return None
    root = ElementTree.fromstring(data)
    return root if root.tag == "ComicInfo" else None


def _read_comicinfo_title(archive: zipfile.ZipFile) -> str:
    root = _read_comicinfo(archive)
    return " ".join((root.findtext("Title") or "").split()) if root is not None else ""


def comicinfo_number(path: Path) -> float | None:
    """The standard ComicInfo Number field from a CBZ/ZIP, if numeric."""
    return _comicinfo_number_cache.get(path, _read_comicinfo_number, None)


def _read_comicinfo_number(archive: zipfile.ZipFile) -> float | None:
    root = _read_comicinfo(archive)
    if root is None:
        return None
    raw = (root.findtext("Number") or "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def archive_chapter_numbers(path: Path) -> frozenset[float] | None:
    """Explicit chapter labels from every image member of a CBZ/ZIP.

    A book number can refer to a different edition (e.g. a two-in-one English
    volume). Only complete labeling overrides metadata coverage; generic page
    numbers, partly labeled archives and unsupported formats use the existing
    filename/metadata fallback. Read the ZIP directory, never page payloads.
    """
    return _chapter_numbers_cache.get(path, _read_archive_chapter_numbers, None)


def _read_archive_chapter_numbers(archive: zipfile.ZipFile) -> frozenset[float] | None:
    numbers: set[float] = set()
    for _, name in archive_pages(archive_files(archive.infolist())):
        if not has_chapter_marker(name.stem):
            return None
        number = parse_chapter_number(name.stem)
        if number is None:
            return None
        numbers.add(number)
    return frozenset(numbers) if numbers else None


PageSignature = tuple[tuple[int, int], ...]


def page_bytes_signature(pages: list[bytes]) -> PageSignature:
    """Ordered image identity used to reject chapter-number aliases.

    ZIP CRCs are CRC32 values of the uncompressed member payload, so this is
    directly comparable to :func:`archive_page_signature` without reopening
    or hashing every existing image.  Member names and ComicInfo are ignored.
    """
    return tuple((zlib.crc32(page) & 0xFFFFFFFF, len(page)) for page in pages)


def archive_page_signature(path: Path) -> PageSignature | None:
    """Ordered page CRC/size pairs for a CBZ/ZIP, excluding metadata."""
    return _page_signature_cache.get(path, _read_archive_page_signature, None)


def _read_archive_page_signature(archive: zipfile.ZipFile) -> PageSignature | None:
    signature = tuple(
        (member.CRC, member.file_size)
        for member, _ in archive_pages(archive_files(archive.infolist()))
    )
    return signature or None


# Optional provenance binds original identities to the actual archived pages.
# Keep archive_page_signature's byte identity contract unchanged for its callers.
PAGE_PROVENANCE_NAME = "MangarrPageProvenance.json"


def archive_source_page_signature(path: Path) -> PageSignature | None:
    return _source_page_signature_cache.get(path, _read_archive_source_page_signature, None)


def _read_archive_source_page_signature(archive: zipfile.ZipFile) -> PageSignature | None:
    stored = _read_archive_page_signature(archive)
    return read_archive_source_page_signature(archive, stored) if stored else None


def read_archive_source_page_signature(
    archive: zipfile.ZipFile, stored: PageSignature
) -> PageSignature | None:
    """Validate provenance against the pages of an already opened archive."""
    try:
        members = [m for m in archive.infolist() if m.filename == PAGE_PROVENANCE_NAME]
        if not members:
            return stored  # archives created before optimization or with it off
        if len(members) != 1:
            return None
        member = members[0]
        if (member.file_size > MAX_COMICINFO_BYTES or member.compress_type not in
                {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
            return None
        with archive.open(member) as stream:
            data = stream.read(MAX_COMICINFO_BYTES + 1)
        if len(data) > MAX_COMICINFO_BYTES:
            return None
        payload = json.loads(data)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return None
        original = payload.get("original")
        if (payload.get("archived") != [list(pair) for pair in stored]
                or not isinstance(original, list) or len(original) != len(stored)):
            return None
        if not all(isinstance(pair, list) and len(pair) == 2
                   and all(type(v) is int for v in pair)
                   and 0 <= pair[0] <= 0xFFFFFFFF and pair[1] > 0 for pair in original):
            return None
        return tuple(tuple(pair) for pair in original)
    except (OSError, KeyError, ValueError, RuntimeError, EOFError, RecursionError,
        zipfile.BadZipFile, zlib.error):
        return None


def find_series_media_files(
    folders: list[Path],
    chapters: list[Chapter],
    volume_offsets: dict[str, int] | None = None,
    *, include_pdfs: bool = True,
) -> list[MediaFile]:
    """Discover a series' media, applying optional physical-volume offsets.

    An offset entry scopes a shared folder as well as translating its volume
    numbers: physical volume ``N + offset`` is exposed as local volume ``N``.
    Files belonging to other volume ranges in that shared folder are hidden
    from scan, map and cleanup operations for this series.
    """
    offsets = volume_offsets or {}
    tracked_volumes = {chapter.volume for chapter in chapters if chapter.volume is not None}
    found: dict[str, MediaFile] = {}
    for raw_folder in folders:
        folder = Path(raw_folder)
        if not folder.exists():
            continue
        try:
            resolved_folder = folder.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        key = str(resolved_folder)
        scoped = key in offsets
        offset = offsets.get(key, 0)
        for media in find_media_files(folder, include_pdfs=include_pdfs):
            if scoped and media.volume_number is not None:
                local_volume = media.volume_number - offset
                if local_volume not in tracked_volumes:
                    continue
                parsed = media.coverage
                media = replace(
                    media, volume_number=local_volume,
                    coverage=replace(parsed, volume=(local_volume, local_volume)) if parsed else None,
                )
            elif scoped and media.coverage and media.coverage.volume:
                low, high = media.coverage.volume
                local = (low - offset, high - offset)
                # A pack crossing series boundaries cannot safely be assigned
                # in its entirety to either part of a shared folder.
                scoped_volumes = {v for v in tracked_volumes if local[0] <= v <= local[1]}
                if len(scoped_volumes) != local[1] - local[0] + 1:
                    continue
                media = replace(media, coverage=replace(media.coverage, volume=local))
            try:
                resolved_media = media.path.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            # A symlink may point outside the folder (a seeding or shared copy).
            # Adopt it; ComicInfo editing separately refuses files outside the
            # series folders, and aliases resolve to one key and one owner set.
            found.setdefault(str(resolved_media), media)
    return list(found.values())


def match_files(
    media: list[MediaFile], chapters: list[Chapter], *,
    manual_pdfs: dict[tuple[int, int], list[Chapter]] | None = None,
) -> MatchResult:
    """Match single chapters and full chapter/volume spans to tracked rows."""
    by_number = {c.number: c for c in chapters}
    chapters_in_volume: dict[int, list[Chapter]] = {}
    for c in chapters:
        if c.volume is not None:
            chapters_in_volume.setdefault(c.volume, []).append(c)

    matched: list[MatchedFile] = []
    unmatched: list[MediaFile] = []
    for mf in media:
        mapped = manual_pdfs.get(pdf_file_identity(mf.path)) if manual_pdfs and mf.is_pdf else None
        if mapped:
            single = mapped[0] if len(mapped) == 1 else None
            renamed = replace(mf, chapter_number=single.number if single else None, volume_number=None)
            matched.append(MatchedFile(renamed, single, None, mapped))
            continue
        parsed = mf.coverage
        if parsed is not None and parsed.ambiguous:
            unmatched.append(mf)
            continue
        # A span is never reduced to its first tracked chapter or a single
        # ComicInfo Number. Explicit page labels still override edition maps.
        if parsed is not None and parsed.is_range:
            content = None if mf.is_dir else archive_chapter_numbers(mf.path)
            covered = [c for c in chapters if (
                c.number in content if content is not None else parsed.covers(c.number, c.volume)
            )]
            matched.append(MatchedFile(
                media=mf, chapter=None, volume=mf.volume_number,
                covered_chapters=covered,
                content_chapters=content if content is not None else (
                    frozenset(c.number for c in covered) if parsed.chapter else None
                ),
            ))
            continue
        chapter = by_number.get(mf.chapter_number) if mf.chapter_number is not None else None
        if chapter is None and mf.volume_number is None and not mf.is_dir:
            embedded_number = comicinfo_number(mf.path)
            embedded_chapter = by_number.get(embedded_number)
            if embedded_chapter is not None:
                mf = replace(mf, chapter_number=embedded_number)
                chapter = embedded_chapter
        if chapter is not None:
            matched.append(MatchedFile(media=mf, chapter=chapter, volume=None,
                                       covered_chapters=[chapter]))
        elif mf.volume_number is not None:
            content = None if mf.is_dir else archive_chapter_numbers(mf.path)
            if content is not None:
                covered = [c for c in chapters if c.number in content]
            else:
                covered = chapters_in_volume.get(mf.volume_number, [])
            matched.append(MatchedFile(media=mf, chapter=None, volume=mf.volume_number,
                                       covered_chapters=list(covered), content_chapters=content))
        else:
            unmatched.append(mf)
    return MatchResult(matched=matched, unmatched=unmatched)
