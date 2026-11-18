from zipfile import ZipFile

from mangarr.comicinfo import read_comicinfo, update_comicinfo
from mangarr.metadata_issues import ArchiveOwnership, find_candidates
from mangarr.models import Chapter, RootFolder, Series


def test_macos_archive_metadata_does_not_count_as_pages_or_chapter_labels(tmp_path):
    folder = tmp_path / "Tracked"
    folder.mkdir()
    path = folder / "Tracked Ch. 1.cbz"
    members = {
        "Chapter 1/001.JPEG": b"page",
        "__MACOSX/Chapter 2/001.jpg": b"resource fork in ZIP metadata directory",
        ".AppleDouble/Chapter 3/001.png": b"resource fork in AppleDouble directory",
        "pages\\._Chapter 4.jpg": b"resource fork next to the pages",
        "__macosx\\ComicInfo.xml": b"metadata copy",
    }
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "ComicInfo.xml",
            "<ComicInfo><Number>1</Number><PageCount>2</PageCount></ComicInfo>",
        )
        for name, content in members.items():
            archive.writestr(name, content)

    info = read_comicinfo(path)
    assert info.actual_page_count == 1
    assert not info.labels_multiple_chapters

    chapter = Chapter(id=1, number=1, downloaded=True, file_path=str(path))
    series = Series(
        title=folder.name, root_folder=RootFolder(path=str(tmp_path)), chapters=[chapter],
    )
    ownership = ArchiveOwnership([(chapter.id, chapter.file_path)])
    candidates = find_candidates(series, ownership)
    assert candidates is not None
    [issue] = candidates
    assert issue.field == "page_count"
    assert issue.suggested_value == "1"

    updated = update_comicinfo(path, {"page_count": 1}, expected_revision=info.revision)
    assert updated.page_count == "1"
    assert updated.actual_page_count == 1
    assert not updated.labels_multiple_chapters
    with ZipFile(path) as archive:
        assert {name: archive.read(name) for name in members} == members
