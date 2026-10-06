"""Library files written through a temporary file must get a normal file
mode: temp files are created 0600 and an atomic rename keeps that, which locks
out a reader (Kavita, Komga) running as another user."""

import os
import zipfile

from mangarr.library.importer import _pack_images, place_file
from mangarr.library.volume_merge import merge_chapter_archives
from mangarr.util import NEW_FILE_MODE


def mode(path) -> int:
    return path.stat().st_mode & 0o777


def test_new_file_mode_follows_the_umask():
    current = os.umask(0)
    os.umask(current)
    assert NEW_FILE_MODE == 0o666 & ~current


def test_packed_loose_images_get_a_normal_mode(tmp_path):
    pages = tmp_path / "Chapter 1"
    pages.mkdir()
    for n in (1, 2):
        (pages / f"{n:03d}.jpg").write_bytes(b"\xff\xd8\xff page")
    dest = tmp_path / "Series - Ch. 0001.cbz"

    _pack_images(pages, dest)

    assert mode(dest) == NEW_FILE_MODE != 0o600


def test_merged_volume_gets_a_normal_mode(tmp_path):
    chapters = []
    for n in (1, 2):
        path = tmp_path / f"Series - Ch. {n:04d}.cbz"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("001.jpg", b"\xff\xd8\xff page")
        chapters.append((float(n), path))
    dest = tmp_path / "Series - Vol. 01.cbz"

    merge_chapter_archives(chapters, dest, "Series", 1)

    assert mode(dest) == NEW_FILE_MODE != 0o600


def test_copy_mode_still_keeps_the_source_files_mode(tmp_path):
    src = tmp_path / "payload.cbz"
    src.write_bytes(b"archive")
    src.chmod(0o640)
    dest = tmp_path / "Series - Ch. 0001.cbz"

    place_file(src, dest, "copy")

    assert mode(dest) == 0o640
