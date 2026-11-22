"""Archive caches must follow the inode, not just a path, size, and mtime."""

import os
import zipfile
from pathlib import Path

from mangarr.library import matcher
from mangarr.library.importer import import_torrent_payload
from mangarr.library.naming import DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME
from mangarr.models import Chapter, Series

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _zip_bytes(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for name, data in members.items():
            archive.writestr(zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), data)


def _replace_preserving_size_and_mtime(path: Path, members: dict[str, bytes]) -> None:
    """Swap in a new inode the way rsync or a copy2-based tool can."""
    before = path.stat()
    staged = path.with_name(f".{path.name}.new")
    _zip_bytes(staged, members)
    os.utime(staged, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(staged, path)
    after = path.stat()
    assert after.st_ino != before.st_ino
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def _info(number: str, title: str = "Alpha") -> dict[str, bytes]:
    xml = f"<ComicInfo><Title>{title}</Title><Number>{number}</Number></ComicInfo>"
    return {"ComicInfo.xml": xml.encode(), "001.png": PNG}


def test_comicinfo_caches_reread_a_replacement_inode(tmp_path):
    path = tmp_path / "Unlabeled.cbz"
    _zip_bytes(path, _info("2", "Alpha"))
    assert matcher.comicinfo_number(path) == 2
    assert matcher.comicinfo_title(path) == "Alpha"

    _replace_preserving_size_and_mtime(path, _info("3", "Bravo"))

    assert matcher.comicinfo_number(path) == 3
    assert matcher.comicinfo_title(path) == "Bravo"


def test_page_caches_reread_a_replacement_inode(tmp_path):
    path = tmp_path / "Pages.cbz"
    _zip_bytes(path, {"c002_001.png": PNG + b"a"})
    assert matcher.archive_chapter_numbers(path) == frozenset({2.0})
    first = matcher.archive_page_signature(path)
    assert matcher.archive_source_page_signature(path) == first

    _replace_preserving_size_and_mtime(path, {"c003_001.png": PNG + b"b"})

    assert matcher.archive_chapter_numbers(path) == frozenset({3.0})
    second = matcher.archive_page_signature(path)
    assert second != first
    assert matcher.archive_source_page_signature(path) == second


def test_torrent_import_matches_the_inode_it_validates(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    source = payload / "Unlabeled.cbz"
    _zip_bytes(source, _info("2"))
    series = Series(id=1, title="Tracked", folder_name="")
    chapters = [Chapter(id=n, series_id=1, number=float(n), title="") for n in (2, 3)]
    # An earlier scan or import cached the previous payload's Number.
    assert matcher.comicinfo_number(source) == 2

    _replace_preserving_size_and_mtime(source, _info("3"))
    [(dest, chapter, _)] = import_torrent_payload(
        payload, series, chapters, tmp_path / "library", DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME,
    )

    assert chapter is chapters[1]
    assert os.path.samefile(dest, source)
