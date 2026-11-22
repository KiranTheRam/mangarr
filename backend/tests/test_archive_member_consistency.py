from zipfile import ZipFile

import pytest

from mangarr.comicinfo import ComicInfoError, read_comicinfo, update_comicinfo
from mangarr.library.matcher import (
    archive_chapter_numbers, archive_page_signature, comicinfo_number, comicinfo_title,
    page_bytes_signature,
)
from mangarr.library.scanner import scan_series
from mangarr.library.volume_merge import merge_chapter_archives
from mangarr.metadata_issues import ArchiveOwnership, find_candidates
from mangarr.models import Chapter, RootFolder, Series


def test_generic_archive_is_matched_using_the_metadata_the_editor_reads(tmp_path):
    folder = tmp_path / "Tracked"
    folder.mkdir()
    path = folder / "Bonus.cbz"
    ignored_metadata = "<ComicInfo><Number>2</Number><Title>Copy</Title></ComicInfo>"
    with ZipFile(path, "w") as archive:
        archive.writestr("__MACOSX/ComicInfo.xml", ignored_metadata)
        archive.writestr("@EADIR\\ComicInfo.xml", ignored_metadata)
        archive.writestr("pages\\ComicInfo.xml", "<ComicInfo><Number>1</Number><Title>Story</Title></ComicInfo>")
        archive.writestr("001.jpg", b"page")

    chapter = Chapter(id=1, number=1)
    other = Chapter(id=2, number=2)
    series = Series(
        id=1, title=folder.name, root_folder=RootFolder(path=str(tmp_path)), chapters=[chapter, other],
    )
    scan_series(series, series.chapters, [folder], root=tmp_path)

    assert chapter.downloaded and chapter.file_path == str(path)
    assert not other.downloaded
    assert comicinfo_number(path) == 1
    assert comicinfo_title(path) == "Story"
    assert read_comicinfo(path).number == "1"
    assert find_candidates(series, ArchiveOwnership([(chapter.id, chapter.file_path)])) == []


def test_distinct_library_metadata_entries_are_ambiguous_to_both_readers(tmp_path):
    path = tmp_path / "Chapter.cbz"
    with ZipFile(path, "w") as archive:
        archive.writestr("ComicInfo.xml", "<ComicInfo><Number>1</Number></ComicInfo>")
        archive.writestr("nested/COMICINFO.XML", "<ComicInfo><Number>2</Number></ComicInfo>")

    assert comicinfo_number(path) is None
    with pytest.raises(ComicInfoError, match="multiple ComicInfo.xml"):
        read_comicinfo(path)


def test_hidden_and_nas_members_are_not_pages_but_survive_metadata_edits(tmp_path):
    path = tmp_path / "Tracked Ch. 1.cbz"
    page = b"story page"
    excluded_members = {
        ".thumb.jpg": b"hidden thumbnail",
        ".previews/Chapter 2.jpg": b"hidden preview",
        "@eaDIR/001.jpg/SYNOPHOTO_THUMB_S.jpg": b"Synology thumbnail",
        "nested/.@__thumb/Chapter 2.jpg": b"QNAP thumbnail",
        ".zfs\\snapshot\\copy.png": b"snapshot",
    }
    with ZipFile(path, "w") as archive:
        archive.writestr("c001-p001.jpg", page)
        archive.writestr("ComicInfo.xml", "<ComicInfo><Number>1</Number><PageCount>4</PageCount></ComicInfo>")
        for name, content in excluded_members.items():
            archive.writestr(name, content)

    assert read_comicinfo(path).actual_page_count == 1
    assert archive_chapter_numbers(path) == {1}
    assert archive_page_signature(path) == page_bytes_signature([page])

    assert update_comicinfo(path, {"page_count": 1}).page_count == "1"
    with ZipFile(path) as archive:
        assert {name: archive.read(name) for name in excluded_members} == excluded_members

    merged = tmp_path / "Volume 1.cbz"
    assert merge_chapter_archives([(1, path)], merged, "Tracked", 1) == 1
    with ZipFile(merged) as archive:
        assert archive.read("0001-0001.jpg") == page


def test_relative_page_names_survive_a_volume_merge_in_reading_order(tmp_path):
    path = tmp_path / "Tracked Ch. 1.cbz"
    with ZipFile(path, "w") as archive:
        archive.writestr("./002.jpg", b"second")
        archive.writestr("001.jpg", b"first")
        archive.writestr("./.thumb.jpg", b"thumbnail")

    assert read_comicinfo(path).actual_page_count == 2
    merged = tmp_path / "Volume 1.cbz"
    assert merge_chapter_archives([(1, path)], merged, "Tracked", 1) == 2
    with ZipFile(merged) as archive:
        assert archive.read("0001-0001.jpg") == b"first"
        assert archive.read("0001-0002.jpg") == b"second"
