"""Import completed torrent payloads into the library.

Handles: single .cbz/.zip/.cbr files, directories of archives, and
directories of loose images (zipped into one CBZ). File→chapter matching is
shared with the library scanner via library.matcher."""

import logging
import os
import re
import shutil
import zipfile
from contextlib import contextmanager
from tempfile import NamedTemporaryFile
from pathlib import Path

from ..models import Chapter, Series
from ..util import NEW_FILE_MODE
from .matcher import (
    IMAGE_EXTS,
    MediaFile,
    archive_page_signature,
    find_media_files,
    match_files,
)
from ..util import natural_key
from .naming import chapter_filename, series_folder, volume_filename

log = logging.getLogger(__name__)

_SEASON_EPISODE = re.compile(
    r"(?:\bseason\s*|\bs\s*)(\d+)\b.*?\b(?:episode|ep)\.?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _dest_ext(media: MediaFile) -> str:
    """Target extension: pack loose images to .cbz, else preserve the archive
    format (normalizing container synonyms)."""
    if media.is_dir:
        return ".cbz"
    return {".zip": ".cbz", ".rar": ".cbr", ".7z": ".cb7"}.get(
        media.path.suffix.lower(), media.path.suffix.lower()
    )


@contextmanager
def _atomic_destination(dest: Path, keep_mode: bool = False):
    """Never expose a partial copy/archive under its library filename.

    The temp file's 0600 is replaced with NEW_FILE_MODE once it is written
    (not before: a umask without owner write would make it unwritable).
    keep_mode leaves the writer's mode, e.g. the source mode copy2 carries."""
    with NamedTemporaryFile(dir=dest.parent, prefix=".mangarr-", suffix=".partial", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        yield temporary
        if not keep_mode:
            temporary.chmod(NEW_FILE_MODE)
        temporary.replace(dest)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_existing(dest: Path) -> None:
    if dest.stat().st_size == 0:
        raise ValueError(f"Existing library file is empty: {dest}")
    if dest.suffix.lower() in {".cbz", ".zip"}:
        try:
            with zipfile.ZipFile(dest) as archive:
                if not archive.namelist() or archive.testzip() is not None:
                    raise ValueError("empty or damaged archive")
        except (zipfile.BadZipFile, ValueError) as exc:
            raise ValueError(f"Existing library archive is damaged; repair or remove it before retrying: {dest}") from exc


def place_file(src: Path, dest: Path, mode: str) -> None:
    """Put a payload file into the library. Hardlink mode keeps the torrent
    seeding without doubling disk use; it needs src and dest on one
    filesystem, so cross-device (and any other) failure falls back to copy."""
    if mode == "hardlink":
        try:
            os.link(src, dest)
            log.info("Hardlinked %s -> %s", src.name, dest)
            return
        except OSError as exc:
            log.warning("hardlink %s -> %s failed (%s); copying instead",
                        src.name, dest, exc)
    with _atomic_destination(dest, keep_mode=True) as temporary:
        shutil.copy2(src, temporary)
        if temporary.stat().st_size != src.stat().st_size:
            raise OSError("Incomplete library copy")
    log.info("Imported %s -> %s", src.name, dest)


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(folder.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _preflight_numbering(media: list[MediaFile]) -> None:
    """Refuse filenames that expose a season-local episode as a chapter.

    ``Ch.081 - [Season 2] Ep.1`` is safe because the explicit global chapter
    wins. ``[Season 2] Ep.1`` parses as chapter 1 and would silently overwrite
    or alias Season 1, so the whole batch is stopped before any file is placed.
    """
    for item in media:
        match = _SEASON_EPISODE.search(item.path.stem)
        if not match or int(match.group(1)) <= 1 or item.chapter_number is None:
            continue
        episode = float(match.group(2))
        if item.chapter_number == episode:
            raise ValueError(
                f"possible per-season numbering reset in {item.path.name}: "
                f"season {match.group(1)} episode {match.group(2)} would map to "
                f"chapter {item.chapter_number:g}; use overall-series numbers"
            )


def _preflight_duplicates(result, chapters: list[Chapter]) -> None:
    """Reject ambiguous chapter mappings and exact cross-number duplicates."""
    by_number: dict[float, list[MediaFile]] = {}
    signature_numbers: dict[tuple[tuple[int, int], ...], tuple[float, Path]] = {}

    for matched in result.matched:
        if matched.chapter is None:
            continue
        number = matched.chapter.number
        by_number.setdefault(number, []).append(matched.media)
        signature = archive_page_signature(matched.media.path)
        if signature is None:
            continue
        previous = signature_numbers.get(signature)
        if previous is not None and previous[0] != number:
            raise ValueError(
                f"torrent contains identical page images for chapters "
                f"{previous[0]:g} ({previous[1].name}) and {number:g} "
                f"({matched.media.path.name})"
            )
        signature_numbers[signature] = (number, matched.media.path)

    collisions = {
        number: files for number, files in by_number.items() if len(files) > 1
    }
    if collisions:
        number, files = next(iter(collisions.items()))
        names = ", ".join(item.path.name for item in files[:3])
        raise ValueError(
            f"torrent maps multiple files to chapter {number:g} ({names}); "
            "possible per-season numbering reset"
        )

    for chapter in chapters:
        if not chapter.downloaded or not chapter.file_path:
            continue
        signature = archive_page_signature(Path(chapter.file_path))
        if signature is None:
            continue
        candidate = signature_numbers.get(signature)
        if candidate is not None and candidate[0] != chapter.number:
            raise ValueError(
                f"torrent chapter {candidate[0]:g} ({candidate[1].name}) has the "
                f"same page images as existing chapter {chapter.number:g}"
            )


def import_torrent_payload(
    content_path: Path,
    series: Series,
    chapters: list[Chapter],
    library_root: Path,
    template: str,
    template_no_volume: str,
    import_mode: str = "hardlink",
) -> list[tuple[Path, Chapter | None, int | None]]:
    """Copies/renames payload files into the library. Returns (dest, matched
    chapter, volume) triples; chapter is None for volume archives that span
    chapters — those carry the parsed volume number instead."""
    folder = library_root / (series.folder_name or series_folder(series.title))
    folder.mkdir(parents=True, exist_ok=True)
    imported: list[tuple[Path, Chapter | None, int | None]] = []
    media = find_media_files(content_path)
    _preflight_numbering(media)
    result = match_files(media, chapters)
    _preflight_duplicates(result, chapters)

    def place(media: MediaFile, chapter: Chapter | None, volume: int | None) -> None:
        ext = _dest_ext(media)
        if chapter is not None:
            dest_name = Path(
                chapter_filename(template, template_no_volume, series.title,
                                 chapter.number, chapter.volume, chapter.title)
            ).stem + ext
        elif volume is not None:
            dest_name = volume_filename(series_folder(series.title), volume, ext)
        else:
            dest_name = f"{series_folder(series.title)} - {media.path.stem}{ext}"
        dest = folder / dest_name
        # A qBittorrent category must normally live outside the library, but
        # older configurations sometimes downloaded straight into it. Do not
        # create a second hardlink/copy beside an already-valid source file.
        if not media.is_dir and _inside(media.path, folder):
            dest = media.path
            _validate_existing(dest)
        elif dest.exists():
            _validate_existing(dest)
        else:
            if media.is_dir:
                _pack_images(media.path, dest)
            else:
                place_file(media.path, dest, import_mode)
        imported.append((dest, chapter, volume if chapter is None else None))

    for mf in result.matched:
        place(mf.media, mf.chapter, mf.volume)
    for media in result.unmatched:
        place(media, None, None)
    return imported


def _pack_images(img_dir: Path, dest: Path) -> None:
    images = sorted(
        (p for p in img_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS),
        key=lambda p: natural_key(p.name),  # 1, 2, 10 — not 1, 10, 2
    )
    with _atomic_destination(dest) as temporary:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as zf:
            for img in images:
                zf.write(img, img.name)
    log.info("Packed %s (%d images) -> %s", img_dir, len(images), dest)
