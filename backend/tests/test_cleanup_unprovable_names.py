"""Library Cleanup must not offer a file for deletion on the strength of
a filename parse it can't trust.

The parser takes the first number it finds, so "Ch. 12-15" reads as chapter
12 and "v01 (2009) (Digital) (c1fi7)" as chapter 1. Cleanup used to group
those with the real single chapter file and pre-select them for deletion."""

import zipfile

import pytest

from mangarr.library.cleanup import analyze, apply_cleanup
from mangarr.library.naming import DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME
from mangarr.models import Chapter, Series

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def make(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("001.png", PNG)


def chapters(numbers, volume_size=None):
    """Chapters with ids; volume_size=5 puts 1-5 in v1, 6-10 in v2, …"""
    return [Chapter(id=i + 1, series_id=1, number=float(n),
                    volume=(n - 1) // volume_size + 1 if volume_size else None)
            for i, n in enumerate(numbers)]


def own(chs, number, path):
    ch = next(c for c in chs if c.number == number)
    ch.downloaded, ch.file_path = True, str(path)


def plan(chs, folder, title="S"):
    return analyze(Series(id=1, title=title, folder_name=""), chs, [folder],
                   DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME)


def default_deletions(p):
    return sorted([f.name for g in p.groups for f in g.files if not f.keep] + [
        f.name for f in [*p.orphans, *p.overlaps] if not f.keep
    ])


@pytest.mark.parametrize("files,numbers,volume_size,owned", [
    # the audit's repro: a range file next to its first chapter
    (["S - Ch. 0012.cbz", "S - Ch. 12-15.cbz"], range(12, 16), None,
     {12: "S - Ch. 0012.cbz"}),
    # the audit's repro: the "(c1fi7)" group tag reads as chapter 1
    (["S - Ch. 0001.cbz", "S v01 (2009) (Digital) (c1fi7).cbz"], range(1, 9), 8,
     {1: "S - Ch. 0001.cbz"}),
    # a title that starts with a chapter-like token reads as chapter 3
    (["C3 - Ch. 0003.cbz", "C3 - Cube x Cursed x Curious 012.cbz"], (3, 12), None,
     {3: "C3 - Ch. 0003.cbz"}),
    # a bare numeric range reads as its last chapter
    (["S - Ch. 0010.cbz", "S 001-010.cbz"], range(1, 11), None,
     {10: "S - Ch. 0010.cbz"}),
    # a volume range reads as its first volume; metadata only knows v1
    (["S - Vol. 01.cbz", "S v01-03 (Digital).cbz"], range(1, 6), 5,
     {n: "S - Vol. 01.cbz" for n in range(1, 6)}),
    # a bracketed chapter span on a volume archive
    (["S - Ch. 0001.cbz", "S v01 (c001-006).cbz"], range(1, 7), 6,
     {1: "S - Ch. 0001.cbz"}),
])
def test_cleanup_never_offers_a_file_it_cannot_prove_is_a_copy(
    tmp_path, files, numbers, volume_size, owned
):
    for name in files:
        make(tmp_path / name)
    chs = chapters(numbers, volume_size)
    for number, name in owned.items():
        own(chs, number, tmp_path / name)
    p = plan(chs, tmp_path)
    assert p.groups == []
    assert default_deletions(p) == []


def test_apply_does_not_trust_a_misparsed_name_as_the_surviving_copy(tmp_path):
    real = tmp_path / "C3 - Ch. 0003.cbz"
    make(real)
    make(tmp_path / "C3 - Cube x Cursed x Curious 012.cbz")
    chs = chapters((3, 12))
    own(chs, 3, real)
    result = apply_cleanup(Series(id=1, title="C3"), chs, [tmp_path], [str(real)])
    assert (result.deleted, result.skipped) == (0, 1)
    assert real.exists() and chs[0].file_path == str(real)


@pytest.mark.parametrize("title,keeper,duplicate,number", [
    # numbered titles still dedupe plain copies of one chapter
    ("86", "86 - Ch. 0003.cbz", "86 c003 (Digital).cbz", 3),
    ("Kaiju No. 8", "Kaiju No. 8 - Ch. 0012.cbz", "Kaiju No. 8 c012 (Digital).cbz", 12),
    ("Mob Psycho 100", "Mob Psycho 100 - Ch. 0005.cbz", "Mob Psycho 100 - Ch. 005 [Group].cbz", 5),
    ("2.5 Dimensional Seduction", "2.5 Dimensional Seduction - Ch. 0010.cbz",
     "2.5 Dimensional Seduction c010.cbz", 10),
])
def test_cleanup_still_groups_plain_copies_of_numbered_titles(
    tmp_path, title, keeper, duplicate, number
):
    make(tmp_path / keeper)
    make(tmp_path / duplicate)
    chs = chapters((number,))
    own(chs, number, tmp_path / keeper)
    p = plan(chs, tmp_path, title=title)
    assert [g.label for g in p.groups] == [f"Chapter {number}"]
    assert default_deletions(p) == [duplicate]


def test_duplicate_volume_archives_still_group(tmp_path):
    make(tmp_path / "S v01.cbz")
    make(tmp_path / "S - Vol. 01.cbz")
    chs = chapters(range(1, 4), 3)
    for n in (1, 2, 3):
        own(chs, n, tmp_path / "S v01.cbz")
    p = plan(chs, tmp_path)
    assert [g.label for g in p.groups] == ["Volume 1"]
    assert default_deletions(p) == ["S - Vol. 01.cbz"]
