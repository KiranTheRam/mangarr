"""Import completed torrent payloads into the library.

Handles: single .cbz/.zip/.cbr files, directories of archives, and
directories of loose images (zipped into one CBZ)."""

import logging
import shutil
import zipfile
from pathlib import Path

from ..models import Chapter, Series
from ..util import parse_chapter_number, parse_volume_number
from .naming import chapter_filename, series_folder

log = logging.getLogger(__name__)

ARCHIVE_EXTS = {".cbz", ".zip", ".cbr", ".rar", ".cb7", ".7z"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}


def _dest_ext(src: Path) -> str:
    return {".zip": ".cbz", ".rar": ".cbr", ".7z": ".cb7"}.get(src.suffix.lower(), src.suffix.lower())


def find_importable_files(content_path: Path) -> list[Path]:
    if content_path.is_file():
        return [content_path] if content_path.suffix.lower() in ARCHIVE_EXTS else []
    return sorted(
        p for p in content_path.rglob("*") if p.is_file() and p.suffix.lower() in ARCHIVE_EXTS
    )


def find_image_dirs(content_path: Path) -> list[Path]:
    """Directories that directly contain images (a chapter/volume as loose pages)."""
    if not content_path.is_dir():
        return []
    dirs = set()
    for p in content_path.rglob("*"):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            dirs.add(p.parent)
    return sorted(dirs)


def import_torrent_payload(
    content_path: Path,
    series: Series,
    chapters: list[Chapter],
    library_root: Path,
    template: str,
    template_no_volume: str,
) -> list[tuple[Path, Chapter | None]]:
    """Copies/renames payload files into the library. Returns (dest, matched
    chapter) pairs; chapter is None for volume archives that span chapters."""
    folder = library_root / (series.folder_name or series_folder(series.title))
    folder.mkdir(parents=True, exist_ok=True)
    imported: list[tuple[Path, Chapter | None]] = []
    by_number = {c.number: c for c in chapters}

    for src in find_importable_files(content_path):
        stem = src.stem
        ch_num = parse_chapter_number(stem)
        vol_num = parse_volume_number(stem)
        chapter = by_number.get(ch_num) if ch_num is not None else None

        if chapter is not None:
            name = chapter_filename(
                template, template_no_volume, series.title, chapter.number,
                chapter.volume, chapter.title,
            )
            dest = folder / (Path(name).stem + _dest_ext(src))
        elif vol_num is not None:
            dest = folder / f"{series_folder(series.title)} - Vol. {vol_num:02d}{_dest_ext(src)}"
        else:
            dest = folder / (series_folder(series.title) + " - " + src.stem + _dest_ext(src))

        if not dest.exists():
            shutil.copy2(src, dest)
            log.info("Imported %s -> %s", src.name, dest)
        imported.append((dest, chapter))

    # loose image directories → zip each into a CBZ
    for img_dir in find_image_dirs(content_path):
        label = img_dir.name if img_dir != content_path else content_path.name
        vol_num = parse_volume_number(label)
        ch_num = parse_chapter_number(label)
        chapter = by_number.get(ch_num) if ch_num is not None else None
        if chapter is not None:
            dest = folder / chapter_filename(
                template, template_no_volume, series.title, chapter.number,
                chapter.volume, chapter.title,
            )
        elif vol_num is not None:
            dest = folder / f"{series_folder(series.title)} - Vol. {vol_num:02d}.cbz"
        else:
            dest = folder / f"{series_folder(series.title)} - {label}.cbz"
        if not dest.exists():
            images = sorted(
                p for p in img_dir.iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS
            )
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
                for img in images:
                    zf.write(img, img.name)
            log.info("Packed %s (%d images) -> %s", img_dir, len(images), dest)
        imported.append((dest, chapter))

    return imported
