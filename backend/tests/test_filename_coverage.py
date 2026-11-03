"""Ranges and release tags must mean the same thing at selection and import."""

import zipfile

import pytest

from mangarr.filename import parse_filename
from mangarr.library.cleanup import analyze, apply_cleanup
from mangarr.library.importer import import_torrent_payload
from mangarr.library.matcher import find_media_files, find_series_media_files, match_files
from mangarr.library.naming import DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME
from mangarr.library.rename import plan_renames
from mangarr.library.scanner import scan_series
from mangarr.models import Chapter, Series
from mangarr.torrent_selection import coverage_from_text, torrent_coverage
from mangarr.util import parse_chapter_number, parse_volume_number
from tests.test_torrent_selection import torrent_with


def chapters():
    return [Chapter(id=i + 1, series_id=1, number=n, volume=1 if n <= 6 else 2)
            for i, n in enumerate([1., 2., 2.5, 3., 4., 5., 6., 7., 8., 9., 10., 12., 13., 14., 15.])]


def archive(path, labels=None, number=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i, label in enumerate(labels or ["001.png", "002.png"]):
            zf.writestr(label, f"page {i}".encode())
        if number is not None:
            zf.writestr("ComicInfo.xml", f"<ComicInfo><Number>{number}</Number></ComicInfo>")


CASES = [
    ("Series - Ch. 12-15", {12., 13., 14., 15.}),
    ("Series v01-03 (Digital)", {1., 2., 2.5, 3., 4., 5., 6., 7., 8., 9., 10., 12., 13., 14., 15.}),
    ("Akira v01 (2009) (Digital) (c1fi7)", {1., 2., 2.5, 3., 4., 5., 6.}),
    ("Berserk v01 (c001-006)", {1., 2., 2.5, 3., 4., 5., 6.}),
    ("Akira c001-010", {1., 2., 2.5, 3., 4., 5., 6., 7., 8., 9., 10.}),
    ("Series c001–c003 [Digital]", {1., 2., 2.5, 3.}),
    ("Series [Ch. 2.5—3] (Digital)", {2.5, 3.}),
    ("Series 001-003 [Group]", {1., 2., 2.5, 3.}),
    ("Series [v01-v02]", {1., 2., 2.5, 3., 4., 5., 6., 7., 8., 9., 10., 12., 13., 14., 15.}),
    ("Series v02 [c001-c003]", {1., 2., 2.5, 3.}),  # explicit chapter bounds beat edition maps
    ("Series c002 [c1fi7]", {2.}),
    ("Series [c1fi7]", set()),
    ("Series [v1group]", set()),
    ("Series c010-c001", set()),
    ("Series v03-01", set()),
    ("Series c001 [c002]", set()),
    ("Series c001 c003", set()),
]


@pytest.mark.parametrize("stem,expected", CASES)
def test_match_scan_and_torrent_use_identical_filename_coverage(tmp_path, stem, expected):
    path = tmp_path / (stem + ".cbz")
    archive(path)
    tracked = chapters()
    result = match_files(find_media_files(path), tracked)
    assert {c.number for m in result.matched for c in m.covered_chapters} == expected
    assert coverage_from_text(stem, tracked) == expected
    series = Series(id=1, title="Series", folder_name="", alt_titles="")
    scan_series(series, tracked, [tmp_path])
    assert {c.number for c in tracked if c.downloaded} == expected
    if expected:
        # No broad release-title fallback can inflate a parseable file's span.
        series.title = stem.split()[0]
        assert torrent_coverage(torrent_with(path.name), "Series c001-c999", tracked, series) == expected


def test_single_number_helpers_never_collapse_ranges_or_read_scene_tags():
    assert parse_chapter_number("Series c001-010") is None
    assert parse_volume_number("Series v01-03") is None
    assert parse_chapter_number("Akira v01 (c1fi7)") is None
    assert parse_volume_number("Akira v01 (c1fi7)") == 1
    assert parse_filename("Series c016x6").chapter == (16.6, 16.6)
    assert not parse_filename("Series (2009) [Digital] [c1fi7]").explicit_chapter


@pytest.mark.parametrize("stem", ["Akira c001-010", "Series v01-03 (Digital)", "Berserk v01 (c001-006)"])
def test_import_and_rename_preserve_ranges_even_with_one_tracked_chapter(tmp_path, stem):
    source = tmp_path / "payload" / (stem + ".zip")
    archive(source, number=1)  # a single ComicInfo Number must not erase the range
    tracked = [Chapter(id=1, series_id=1, number=1, volume=1, title="")]
    series = Series(id=1, title="Series", folder_name="Series", alt_titles="")
    [(dest, chapter, _)] = import_torrent_payload(
        source, series, tracked, tmp_path / "library", DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME,
        import_mode="copy",
    )
    assert chapter is None
    assert dest.name == stem + ".cbz"
    assert dest.read_bytes() == source.read_bytes()
    matched = match_files(find_media_files(dest), tracked).matched[0]
    assert matched.covered_chapters == tracked
    tracked[0].downloaded, tracked[0].file_path = True, str(dest)
    assert plan_renames(series, tracked, DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME) == []


def test_archive_page_labels_still_override_volume_range_edition(tmp_path):
    path = tmp_path / "Series v01-03.cbz"
    archive(path, ["Series c009 - p001.png", "Series c010 - p001.png"])
    tracked = chapters()
    matched = match_files(find_media_files(path), tracked).matched[0]
    assert {c.number for c in matched.covered_chapters} == {9., 10.}
    assert matched.content_chapters == {9., 10.}


def test_scan_prefers_single_chapter_and_repeated_scan_is_stable(tmp_path):
    multi = tmp_path / "Series c001-003.cbz"
    single = tmp_path / "Series c002.cbz"
    archive(multi)
    archive(single)
    tracked = chapters()
    series = Series(id=1, title="Series", folder_name="Series", alt_titles="")
    result = scan_series(series, tracked, [tmp_path])
    assert result.matched_chapters == 4  # includes the known decimal extra
    assert all(c.file_path == str(single if c.number == 2 else multi)
               for c in tracked if c.downloaded)
    again = scan_series(series, tracked, [tmp_path])
    assert again.matched_chapters == 0 and again.cleared == 0


def test_shared_folder_volume_range_is_translated_and_scoped(tmp_path):
    path = tmp_path / "Series v06-07.cbz"
    archive(path)
    archive(tmp_path / "Series v05-06.cbz")  # crosses a series boundary
    tracked = chapters()
    media = find_series_media_files([tmp_path], tracked, {str(tmp_path.resolve()): 5})
    assert [m.path for m in media] == [path]
    assert {c.number for m in match_files(media, tracked).matched for c in m.covered_chapters} == {c.number for c in tracked}


@pytest.mark.parametrize("stem", ["Series c001-010", "Series v01-03", "Series v01 [c001-006]", "Series v01 [c1fi7]", "Series c001 [c002]"])
def test_cleanup_does_not_use_ranges_or_ambiguous_tags_as_deletion_proof(tmp_path, stem):
    multi = tmp_path / (stem + ".cbz")
    real = tmp_path / "Series - Ch. 0001.cbz"
    archive(multi)
    archive(real)
    tracked = chapters()
    tracked[0].downloaded, tracked[0].file_path = True, str(real)
    series = Series(id=1, title="Series", folder_name="Series")
    plan = analyze(series, tracked, [tmp_path], DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME)
    assert not [f for group in plan.groups for f in group.files if not f.keep]
    assert all(f.keep for f in [*plan.overlaps, *plan.orphans])
    result = apply_cleanup(series, tracked, [tmp_path], [str(real)])
    assert result.deleted == 0 and result.skipped == 1
    assert real.exists() and multi.exists()


def test_loose_image_range_keeps_decimal_bounds_when_packed(tmp_path):
    source = tmp_path / "payload" / "Series Ch. 2.5-3.5"
    source.mkdir(parents=True)
    (source / "001.png").write_bytes(b"first page")
    (source / "002.png").write_bytes(b"last page")
    tracked = [Chapter(id=i, series_id=1, number=n) for i, n in enumerate([2., 2.5, 3., 3.5, 4.], 1)]
    series = Series(id=1, title="Series", folder_name="Series", alt_titles="")
    [(dest, chapter, volume)] = import_torrent_payload(
        source, series, tracked, tmp_path / "library", DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME,
    )
    assert dest.name == "Series Ch. 2.5-3.5.cbz"
    assert chapter is None and volume is None
    with zipfile.ZipFile(dest) as zf:
        assert zf.read("001.png") == b"first page"
        assert zf.read("002.png") == b"last page"
    assert {c.number for m in match_files(find_media_files(dest), tracked).matched
            for c in m.covered_chapters} == {2.5, 3., 3.5}
    assert torrent_coverage(torrent_with(source.name + "/001.png"), "Series", tracked, series) == {2.5, 3., 3.5}


def test_disk_volume_evidence_includes_every_volume_in_a_pack(tmp_path):
    from mangarr.jobs.tasks import disk_volume_numbers
    from mangarr.models import RootFolder

    folder = tmp_path / "Series"
    archive(folder / "Series v01-03.cbz")
    series = Series(id=1, title="Series", folder_name="Series", alt_titles="",
                    root_folder=RootFolder(path=str(tmp_path)), folder_pinned=True)
    series.chapters = chapters()
    assert disk_volume_numbers(series) == {1, 2, 3}


def test_partial_bracketed_span_does_not_invent_a_whole_volume_catalogue(tmp_path):
    archive(tmp_path / "Series v01 (c001-003).cbz")
    series = Series(id=1, title="Series", folder_name="Series", alt_titles="",
                    total_volumes=1, total_chapters=10, monitored=True)
    result = scan_series(series, [], [tmp_path])
    assert result.added_chapters == 0 and not series.chapters


def test_import_does_not_invent_coverage_from_added_series_title(tmp_path):
    from mangarr.jobs.tasks import _place_torrent_files

    source = tmp_path / "payload" / "Collection.cbz"
    archive(source)
    series = Series(id=1, title="C3", folder_name="C3")
    tracked = [Chapter(id=1, series_id=1, number=3, title="")]
    placed, coverage = _place_torrent_files(
        source, series, tracked, tmp_path / "library", DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME,
    )
    assert len(placed) == 1 and placed[0][1] is None
    assert coverage == {}
