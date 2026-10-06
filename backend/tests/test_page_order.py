"""Pages must keep reading order when names aren't zero-padded, as in many
torrent-imported archives (1.jpg … 10.jpg): a plain name sort reads
1, 10, 11, 2, 3 and a merge then makes that order permanent."""

import zipfile

from mangarr.library.importer import _pack_images
from mangarr.library.volume_merge import merge_chapter_archives

UNPADDED = ["1.jpg", "2.jpg", "3.jpg", "10.jpg", "11.jpg"]


def test_natural_key_orders_numbers_by_value():
    from mangarr.util import natural_key
    names = ["page10.jpg", "page2.jpg", "Page1.jpg", "page1b.jpg", "a/3.jpg", "a/20.jpg", "b/1.jpg"]
    assert sorted(names, key=natural_key) == [
        "a/3.jpg", "a/20.jpg", "b/1.jpg", "Page1.jpg", "page1b.jpg", "page2.jpg", "page10.jpg",
    ]


def test_natural_key_keeps_zero_padded_order():
    from mangarr.util import natural_key
    padded = [f"{n:03d}.jpg" for n in (1, 2, 9, 10, 100)]
    assert sorted(reversed(padded), key=natural_key) == padded


def _pages_of(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        return [archive.read(name) for name in archive.namelist() if name.endswith(".jpg")]


def test_merge_keeps_reading_order_of_unpadded_pages(tmp_path):
    chapter = tmp_path / "Series - Ch. 0001.cbz"
    with zipfile.ZipFile(chapter, "w") as archive:
        for name in reversed(UNPADDED):  # stored order must not matter either
            archive.writestr(name, f"page {name}".encode())
    dest = tmp_path / "Series - Vol. 01.cbz"

    merge_chapter_archives([(1.0, chapter)], dest, "Series", 1)

    assert _pages_of(dest) == [f"page {name}".encode() for name in UNPADDED]


def test_packing_loose_images_keeps_reading_order(tmp_path):
    pages = tmp_path / "Chapter 1"
    pages.mkdir()
    for name in UNPADDED:
        (pages / name).write_bytes(f"page {name}".encode())
    dest = tmp_path / "Series - Ch. 0001.cbz"

    _pack_images(pages, dest)

    assert _pages_of(dest) == [f"page {name}".encode() for name in UNPADDED]
