"""Shared, read-only matching of on-disk files to tracked chapters.

Used by the importer, scanner and cleanup. Volume CBZ/ZIP archives with explicit
chapter labels on every page use those labels instead of assuming the release's
volume number matches the metadata edition. Archive directories and ComicInfo
titles are cached per file version; page images are never read or written."""

import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree

from ..models import Chapter
from ..util import has_chapter_marker, parse_chapter_number, parse_volume_number

ARCHIVE_EXTS = {".cbz", ".zip", ".cbr", ".rar", ".cb7", ".7z"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}


@dataclass
class MediaFile:
    path: Path
    is_dir: bool  # a directory of loose images (one chapter/volume as pages)
    chapter_number: float | None
    volume_number: int | None

    @property
    def label(self) -> str:
        return self.path.name


@dataclass
class MatchedFile:
    media: MediaFile
    chapter: Chapter | None  # the single chapter this file is, if any
    volume: int | None  # set when the file is a whole-volume archive
    covered_chapters: list[Chapter] = field(default_factory=list)
    content_chapters: frozenset[float] | None = None  # authoritative archive labels


@dataclass
class MatchResult:
    matched: list[MatchedFile]
    unmatched: list[MediaFile]


def _name_source(path: Path, is_dir: bool) -> str:
    """The text we parse the chapter/volume number from."""
    return path.name if is_dir else path.stem


def find_media_files(
    content_path: Path, files: Iterable[Path] | None = None
) -> list[MediaFile]:
    """Archives anywhere under content_path, plus directories that directly
    hold loose images. Non-media files (json sidecars, etc.) are ignored.
    Given `files`, only those are considered instead of walking content_path."""
    content_path = Path(content_path)
    media: list[MediaFile] = []

    if files is None:
        if content_path.is_file():
            if content_path.suffix.lower() in ARCHIVE_EXTS:
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
        if suffix in ARCHIVE_EXTS:
            media.append(_media_of(p, is_dir=False))
        elif suffix in IMAGE_EXTS:
            image_dirs.add(p.parent)

    for d in sorted(image_dirs):
        media.append(_media_of(d, is_dir=True))
    return media


def _media_of(path: Path, is_dir: bool) -> MediaFile:
    text = _name_source(path, is_dir)
    volume = parse_volume_number(text)
    chapter = parse_chapter_number(text)
    # a bare volume name ("Volume 01", "v40 (2019)") has no explicit chapter
    # token, so its trailing number is the volume, not a chapter
    if volume is not None and chapter is not None and not has_chapter_marker(text):
        chapter = None
    return MediaFile(path=path, is_dir=is_dir, chapter_number=chapter, volume_number=volume)


# path -> (mtime, size, title); opening every archive on every scan is far too
# slow for a network-mounted library, so each file version is read only once
_comicinfo_cache: dict[str, tuple[float, int, str]] = {}


def comicinfo_title(path: Path) -> str:
    """The standard ComicInfo Title field from a CBZ/ZIP, if present."""
    if path.suffix.lower() not in {".cbz", ".zip"}:
        return ""
    try:
        stat = path.stat()
    except OSError:
        return ""
    cached = _comicinfo_cache.get(str(path))
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    title = _read_comicinfo_title(path)
    _comicinfo_cache[str(path)] = (stat.st_mtime, stat.st_size, title)
    return title


def _read_comicinfo_title(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            member = next(
                (name for name in archive.namelist()
                 if Path(name).name.lower() == "comicinfo.xml"),
                None,
            )
            if member is None:
                return ""
            root = ElementTree.fromstring(archive.read(member))
            return " ".join((root.findtext("Title") or "").split())
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return ""


def archive_chapter_numbers(path: Path) -> frozenset[float] | None:
    """Explicit chapter labels from every image member of a CBZ/ZIP.

    A book number can refer to a different edition (e.g. a two-in-one English
    volume). Only complete labeling overrides metadata coverage; generic page
    numbers, partly labeled archives and unsupported formats use the existing
    filename/metadata fallback. Read the ZIP directory, never page payloads.
    """
    if path.suffix.lower() not in {".cbz", ".zip"}:
        return None
    try:
        stat = path.stat()
        return _archive_chapter_numbers(str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


@lru_cache(maxsize=256)
def _archive_chapter_numbers(path: str, mtime_ns: int, size: int) -> frozenset[float] | None:
    numbers: set[float] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                name = Path(member.filename)
                if member.is_dir() or name.suffix.lower() not in IMAGE_EXTS:
                    continue
                if not has_chapter_marker(name.stem):
                    return None
                number = parse_chapter_number(name.stem)
                if number is None:
                    return None
                numbers.add(number)
    except (OSError, zipfile.BadZipFile):
        return None
    return frozenset(numbers) if numbers else None


def match_files(media: list[MediaFile], chapters: list[Chapter]) -> MatchResult:
    """Match each media file to a chapter (by number) or, for whole-volume
    archives, to every chapter assigned to that volume."""
    by_number = {c.number: c for c in chapters}
    chapters_in_volume: dict[int, list[Chapter]] = {}
    for c in chapters:
        if c.volume is not None:
            chapters_in_volume.setdefault(c.volume, []).append(c)

    matched: list[MatchedFile] = []
    unmatched: list[MediaFile] = []
    for mf in media:
        chapter = by_number.get(mf.chapter_number) if mf.chapter_number is not None else None
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
