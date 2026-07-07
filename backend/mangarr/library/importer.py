"""Import completed torrent payloads into the library.

Handles: single .cbz/.zip/.cbr files, directories of archives, and
directories of loose images (zipped into one CBZ). File→chapter matching is
shared with the library scanner via library.matcher."""

import logging
import os
import shutil
import zipfile
from pathlib import Path

from ..models import Chapter, Series
from .matcher import IMAGE_EXTS, MediaFile, find_media_files, match_files
from .naming import chapter_filename, series_folder, volume_filename

log = logging.getLogger(__name__)


def _dest_ext(media: MediaFile) -> str:
    """Target extension: pack loose images to .cbz, else preserve the archive
    format (normalizing container synonyms)."""
    if media.is_dir:
        return ".cbz"
    return {".zip": ".cbz", ".rar": ".cbr", ".7z": ".cb7"}.get(
        media.path.suffix.lower(), media.path.suffix.lower()
    )


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
    shutil.copy2(src, dest)
    log.info("Imported %s -> %s", src.name, dest)


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
    result = match_files(find_media_files(content_path), chapters)

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
        if not dest.exists():
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
        p for p in img_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
        for img in images:
            zf.write(img, img.name)
    log.info("Packed %s (%d images) -> %s", img_dir, len(images), dest)
