"""Shared, read-only matching of on-disk files to tracked chapters.

Used both by the importer (copying completed downloads into the library) and
the scanner (adopting an existing library in place). Filename-based only — it
never opens or writes files."""

from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass
class MatchResult:
    matched: list[MatchedFile]
    unmatched: list[MediaFile]


def _name_source(path: Path, is_dir: bool) -> str:
    """The text we parse the chapter/volume number from."""
    return path.name if is_dir else path.stem


def find_media_files(content_path: Path) -> list[MediaFile]:
    """Archives anywhere under content_path, plus directories that directly
    hold loose images. Non-media files (json sidecars, etc.) are ignored."""
    content_path = Path(content_path)
    media: list[MediaFile] = []

    if content_path.is_file():
        if content_path.suffix.lower() in ARCHIVE_EXTS:
            media.append(_media_of(content_path, is_dir=False))
        return media

    if not content_path.is_dir():
        return media

    image_dirs: set[Path] = set()
    for p in sorted(content_path.rglob("*")):
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
        elif mf.volume_number is not None and mf.volume_number in chapters_in_volume:
            covered = chapters_in_volume[mf.volume_number]
            matched.append(MatchedFile(media=mf, chapter=None, volume=mf.volume_number,
                                       covered_chapters=list(covered)))
        elif mf.volume_number is not None:
            # a volume archive for a volume we don't have chapter rows for yet;
            # keep the volume tag so callers can still name it, but no coverage
            matched.append(MatchedFile(media=mf, chapter=None, volume=mf.volume_number,
                                       covered_chapters=[]))
        else:
            unmatched.append(mf)
    return MatchResult(matched=matched, unmatched=unmatched)
