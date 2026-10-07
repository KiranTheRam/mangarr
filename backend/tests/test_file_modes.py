"""Library files written through a temporary file must get a normal file
mode: temp files are created 0600 and an atomic rename keeps that, which locks
out a reader (Kavita, Komga) running as another user."""

import os
import zipfile

import pytest

from mangarr.library import importer, volume_merge
from mangarr.util import NEW_FILE_MODE


def mode(path) -> int:
    return path.stat().st_mode & 0o777


def test_new_file_mode_follows_the_umask():
    current = os.umask(0)
    os.umask(current)
    assert NEW_FILE_MODE == 0o666 & ~current


def _loose_pages(tmp_path):
    pages = tmp_path / "Chapter 1"
    pages.mkdir()
    for n in (1, 2):
        (pages / f"{n:03d}.jpg").write_bytes(b"\xff\xd8\xff page")
    return pages


def _chapter_archives(tmp_path):
    chapters = []
    for n in (1, 2):
        path = tmp_path / f"Series - Ch. {n:04d}.cbz"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("001.jpg", b"\xff\xd8\xff page")
        chapters.append((float(n), path))
    return chapters


# 0o644: the usual umask 022. 0o440: a umask without owner write (0227) —
# the mode must be applied after writing, or the write itself fails.
@pytest.mark.parametrize("file_mode", [0o644, 0o440])
def test_packed_loose_images_get_the_new_file_mode(tmp_path, monkeypatch, file_mode):
    monkeypatch.setattr(importer, "NEW_FILE_MODE", file_mode)
    dest = tmp_path / "Series - Ch. 0001.cbz"

    importer._pack_images(_loose_pages(tmp_path), dest)

    assert mode(dest) == file_mode


@pytest.mark.parametrize("file_mode", [0o644, 0o440])
def test_merged_volume_gets_the_new_file_mode(tmp_path, monkeypatch, file_mode):
    monkeypatch.setattr(volume_merge, "NEW_FILE_MODE", file_mode)
    dest = tmp_path / "Series - Vol. 01.cbz"

    volume_merge.merge_chapter_archives(_chapter_archives(tmp_path), dest, "Series", 1)

    assert mode(dest) == file_mode


def test_library_writes_work_under_a_umask_without_owner_write(tmp_path):
    pages = _loose_pages(tmp_path)
    chapters = _chapter_archives(tmp_path)
    src = tmp_path / "payload.cbz"
    src.write_bytes(b"archive")
    previous = os.umask(0o227)  # owner may not write newly created files
    try:
        importer._pack_images(pages, tmp_path / "packed.cbz")
        volume_merge.merge_chapter_archives(chapters, tmp_path / "volume.cbz", "Series", 1)
        importer.place_file(src, tmp_path / "copied.cbz", "copy")
    finally:
        os.umask(previous)

    assert mode(tmp_path / "packed.cbz") == NEW_FILE_MODE
    assert mode(tmp_path / "volume.cbz") == NEW_FILE_MODE
    assert (tmp_path / "copied.cbz").read_bytes() == b"archive"


def test_copy_mode_still_keeps_the_source_files_mode(tmp_path):
    src = tmp_path / "payload.cbz"
    src.write_bytes(b"archive")
    src.chmod(0o640)
    dest = tmp_path / "Series - Ch. 0001.cbz"

    importer.place_file(src, dest, "copy")

    assert mode(dest) == 0o640
