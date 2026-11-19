from zipfile import ZipFile

import pytest

from mangarr.library.scanner import resolve_folders, resolve_volume_offsets, scan_series
from mangarr.metadata_issues import ArchiveOwnership, find_candidates
from mangarr.models import Chapter, RootFolder, Series, SeriesFolder


@pytest.mark.parametrize("volume,expected", [("6", None), ("1", "6"), ("6.0", None)])
@pytest.mark.parametrize("absolute", [False, True])
def test_shared_folder_recommends_physical_volume(tmp_path, volume, expected, absolute):
    root = tmp_path / "library"
    shared = tmp_path / "shared" if absolute else root / "shared"
    (root / "Tracked").mkdir(parents=True)
    shared.mkdir()
    path = shared / "Tracked Vol. 6 Ch. 2.cbz"
    with ZipFile(path, "w") as archive:
        archive.writestr("001.png", b"page")
        archive.writestr("ComicInfo.xml", f"<ComicInfo><Number>2</Number><Volume>{volume}</Volume></ComicInfo>")

    chapter = Chapter(id=1, number=2, volume=1, volume_source="viz")
    extra = SeriesFolder(path=str(shared) if absolute else "shared", volume_offset=5)
    series = Series(
        id=1, title="Tracked", folder_name="Tracked", root_folder=RootFolder(path=str(root)),
        chapters=[chapter], extra_folders=[extra],
    )
    result = scan_series(
        series, series.chapters, resolve_folders(root, series, [extra.path]), resolve_volume_offsets(root, series),
        root=root,
    )

    assert result.matched_chapters == 1
    assert chapter.file_path == str(path)
    findings = find_candidates(series, ArchiveOwnership([(chapter.id, chapter.file_path)]))
    if volume == "6":
        assert findings == []
    else:
        [finding] = findings
        assert finding.field == "volume"
        assert finding.suggested_value == expected
        if expected is None:
            assert finding.kind == "comicinfo_volume_invalid"
        else:
            assert finding.kind == "comicinfo_volume_mismatch"
            assert "local Volume 1 (physical Volume 6" in finding.detail


def test_no_volume_recommendation_for_overlapping_folder_offsets(tmp_path):
    shared = tmp_path / "Tracked" / "shared"
    shared.mkdir(parents=True)
    path = shared / "Tracked Vol. 6 Ch. 2.cbz"
    with ZipFile(path, "w") as archive:
        archive.writestr("001.png", b"page")
        archive.writestr("ComicInfo.xml", "<ComicInfo><Number>2</Number><Volume>6</Volume></ComicInfo>")

    chapter = Chapter(id=1, number=2, volume=1, volume_source="viz", downloaded=True, file_path=str(path))
    series = Series(
        id=1, title="Tracked", folder_name="Tracked", root_folder=RootFolder(path=str(tmp_path)),
        chapters=[chapter], extra_folders=[SeriesFolder(path="Tracked/shared", volume_offset=5)],
    )

    assert find_candidates(series, ArchiveOwnership([(chapter.id, chapter.file_path)])) == []
