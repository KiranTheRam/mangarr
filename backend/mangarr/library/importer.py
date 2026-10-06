"""Import completed torrent payloads into the library.

Handles: single .cbz/.zip/.cbr files, directories of archives, and
directories of loose images (zipped into one CBZ). File→chapter matching is
shared with the library scanner via library.matcher."""

import logging
import os
import shutil
import zipfile
from contextlib import contextmanager
from tempfile import NamedTemporaryFile
from pathlib import Path

from ..models import Chapter, Series
from .matcher import ARCHIVE_EXTS, IMAGE_EXTS, MediaFile, find_media_files, match_files
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


@contextmanager
def _atomic_destination(dest: Path):
    """Never expose a partial copy/archive under its library filename."""
    with NamedTemporaryFile(dir=dest.parent, prefix=".mangarr-", suffix=".partial", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        yield temporary
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
    with _atomic_destination(dest) as temporary:
        shutil.copy2(src, temporary)
        if temporary.stat().st_size != src.stat().st_size:
            raise OSError("Incomplete library copy")
    log.info("Imported %s -> %s", src.name, dest)


def import_torrent_payload(
    content_path: Path,
    series: Series,
    chapters: list[Chapter],
    library_root: Path,
    template: str,
    template_no_volume: str,
    import_mode: str = "hardlink",
    files: list[Path] | None = None,
) -> list[tuple[Path, Chapter | None, int | None]]:
    """Copies/renames payload files into the library. Returns (dest, matched
    chapter, volume) triples; chapter is None for volume archives that span
    chapters — those carry the parsed volume number instead.

    `files` (the torrent's own files) limits the import to those instead of
    everything under content_path, which may be a folder shared with other
    torrents."""
    only = set(files) if files is not None else None
    if only is not None:
        if not only:
            raise ValueError("qBittorrent listed no files to import for this torrent")
        # content_path existing proves nothing when it is the shared folder; a
        # listed archive or page that is gone was moved mid-import, so let the
        # caller retry. Sidecars (.nfo, .txt …) a user deleted don't matter.
        for path in sorted(only):
            if path.suffix.lower() in ARCHIVE_EXTS | IMAGE_EXTS and not path.exists():
                raise FileNotFoundError(f"torrent file missing: {path}")
    folder = library_root / (series.folder_name or series_folder(series.title))
    folder.mkdir(parents=True, exist_ok=True)
    imported: list[tuple[Path, Chapter | None, int | None]] = []
    result = match_files(find_media_files(content_path, only), chapters)

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
        if dest.exists():
            _validate_existing(dest)
        else:
            if media.is_dir:
                _pack_images(media.path, dest, only)
            else:
                place_file(media.path, dest, import_mode)
        imported.append((dest, chapter, volume if chapter is None else None))

    for mf in result.matched:
        place(mf.media, mf.chapter, mf.volume)
    for media in result.unmatched:
        place(media, None, None)
    return imported


def _pack_images(img_dir: Path, dest: Path, only: set[Path] | None = None) -> None:
    images = sorted(
        p for p in img_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and (only is None or p in only)
    )
    with _atomic_destination(dest) as temporary:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as zf:
            for img in images:
                zf.write(img, img.name)
    log.info("Packed %s (%d images) -> %s", img_dir, len(images), dest)
