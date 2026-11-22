"""Pages must keep reading order when names aren't zero-padded, as in many
torrent-imported archives (1.jpg … 10.jpg): a plain name sort reads
1, 10, 11, 2, 3 and a merge then makes that order permanent."""

import zipfile

import pytest

from mangarr.library import volume_merge
from mangarr.library.importer import _pack_images
from mangarr.library.volume_merge import merge_chapter_archives

UNPADDED = ["1.jpg", "2.jpg", "3.jpg", "10.jpg", "11.jpg"]


def test_natural_key_orders_numbers_by_value():
    from mangarr.util import natural_key
    names = ["page10.jpg", "page2.jpg", "Page1.jpg", "page1b.jpg", "a/3.jpg", "a/20.jpg", "b/1.jpg"]
    assert sorted(names, key=natural_key) == [
        "a/3.jpg", "a/20.jpg", "b/1.jpg", "Page1.jpg", "page1b.jpg", "page2.jpg", "page10.jpg",
    ]


def test_natural_key_accepts_non_decimal_digit_characters():
    from mangarr.util import natural_key
    # "①" is a digit to str.isdigit() but not \d: it must sort as text, not crash
    assert sorted(["1①2.jpg", "1.jpg", "10.jpg"], key=natural_key) == ["1.jpg", "1①2.jpg", "10.jpg"]


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


@pytest.mark.parametrize(
    ("method", "page_limit", "volume_limit", "page_count", "error"),
    [
        (zipfile.ZIP_DEFLATED, 7, 32, 3, "expanded size"),
        (zipfile.ZIP_DEFLATED, 8, 15, 3, "expanded size"),
        (zipfile.ZIP_DEFLATED, 8, 32, 1, "page count"),
        (zipfile.ZIP_BZIP2, 8, 32, 3, "unsupported page compression"),
    ],
)
def test_merge_limits_leave_chapter_and_remove_partial(
    tmp_path, monkeypatch, method, page_limit, volume_limit, page_count, error
):
    monkeypatch.setattr(volume_merge, "MAX_MERGE_PAGE_BYTES", page_limit)
    monkeypatch.setattr(volume_merge, "MAX_MERGE_BYTES", volume_limit)
    monkeypatch.setattr(volume_merge, "MAX_MERGE_PAGES", page_count)
    chapter = tmp_path / "chapter.cbz"
    dest = tmp_path / "volume.cbz"
    with zipfile.ZipFile(chapter, "w", method) as archive:
        archive.writestr("1.jpg", b"x" * 8)
        archive.writestr("2.jpg", b"x" * 8)

    with pytest.raises(ValueError, match=error):
        merge_chapter_archives([(1, chapter)], dest, "Series", 1)
    assert list(tmp_path.iterdir()) == [chapter]


def test_merge_limits_zip_directory_before_loading_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(volume_merge, "MAX_ZIP_DIRECTORY_BYTES", 128)
    chapter = tmp_path / "chapter.cbz"
    dest = tmp_path / "volume.cbz"
    with zipfile.ZipFile(chapter, "w") as archive:
        archive.writestr("1.jpg", b"x")
        archive.writestr("x" * 128 + ".txt", b"")
    with pytest.raises(ValueError, match="ZIP directory exceeds"):
        merge_chapter_archives([(1, chapter)], dest, "Series", 1)
    assert list(tmp_path.iterdir()) == [chapter]
