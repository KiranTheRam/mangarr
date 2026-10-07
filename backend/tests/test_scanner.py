import zipfile

from mangarr.library.scanner import find_existing_folder, scan_series
from mangarr.models import Chapter, Series

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def series_with(chapters):
    s = Series(id=1, title="Dandadan", folder_name="Dandadan", alt_titles="")
    return s, chapters


def chs(*specs):
    return [Chapter(id=i + 1, series_id=1, number=float(n), volume=v, downloaded=False,
                    file_path="")
            for i, (n, v) in enumerate(specs)]


def make_cbz(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("001.png", PNG)


class TestScanSeries:
    def test_marks_chapter_files_owned_in_place(self, tmp_path):
        folder = tmp_path / "Dandadan"
        folder.mkdir()
        make_cbz(folder / "Dandadan ch. 1.cbz")
        make_cbz(folder / "Dandadan ch. 2.cbz")
        chapters = chs((1, 1), (2, 1), (3, 1))
        series, _ = series_with(chapters)

        result = scan_series(series, chapters, [folder])

        assert result.matched_chapters == 2
        assert chapters[0].downloaded and chapters[0].file_path.endswith("ch. 1.cbz")
        assert chapters[1].downloaded
        assert not chapters[2].downloaded  # ch3 not on disk
        # files are untouched (still exactly the two we created)
        assert sorted(p.name for p in folder.iterdir()) == [
            "Dandadan ch. 1.cbz", "Dandadan ch. 2.cbz"]

    def test_volume_archive_covers_its_chapters(self, tmp_path):
        folder = tmp_path / "Series"
        folder.mkdir()
        make_cbz(folder / "Series v01.cbz")
        chapters = chs((1, 1), (2, 1), (3, 1), (10, 2))
        series = Series(id=1, title="Series", folder_name="Series", alt_titles="")

        result = scan_series(series, chapters, [folder])

        assert result.volume_files == 1
        assert all(c.downloaded for c in chapters[:3])
        assert not chapters[3].downloaded  # volume 2 not present

    def test_reconciles_missing_files(self, tmp_path):
        folder = tmp_path / "Series"
        folder.mkdir()
        chapters = chs((1, 1))
        chapters[0].downloaded = True
        chapters[0].file_path = str(folder / "gone.cbz")  # file doesn't exist
        series = Series(id=1, title="Series", folder_name="Series", alt_titles="")

        result = scan_series(series, chapters, [folder])

        assert result.cleared == 1
        assert not chapters[0].downloaded and chapters[0].file_path == ""

    def test_unmatched_surfaced(self, tmp_path):
        folder = tmp_path / "Series"
        folder.mkdir()
        make_cbz(folder / "Bonus Artbook.cbz")
        chapters = chs((1, 1))
        series = Series(id=1, title="Series", folder_name="Series", alt_titles="")

        result = scan_series(series, chapters, [folder])
        assert [m.path.name for m in result.unmatched] == ["Bonus Artbook.cbz"]


class TestScanMultipleFolders:
    def test_scans_across_volumes_and_chapters_dirs(self, tmp_path):
        vols = tmp_path / "Series Volumes"
        chaps = tmp_path / "Series Chapters"
        vols.mkdir()
        chaps.mkdir()
        make_cbz(vols / "Series v01.cbz")  # covers ch 1,2,3 (volume 1)
        make_cbz(chaps / "Series ch. 10.cbz")  # exact chapter 10
        chapters = chs((1, 1), (2, 1), (3, 1), (10, 2))
        series = Series(id=1, title="Series", folder_name="Series Volumes", alt_titles="")

        result = scan_series(series, chapters, [vols, chaps])

        assert all(c.downloaded for c in chapters[:3])  # from the volume archive
        assert chapters[3].downloaded  # ch 10 from the chapters dir
        assert chapters[3].file_path.endswith("Series ch. 10.cbz")
        assert result.matched_chapters == 4

    def test_exact_chapter_file_wins_over_volume_archive(self, tmp_path):
        vols = tmp_path / "vols"
        chaps = tmp_path / "chaps"
        vols.mkdir()
        chaps.mkdir()
        make_cbz(vols / "Series v01.cbz")  # volume 1 covers ch 1,2,3
        make_cbz(chaps / "Series ch. 2.cbz")  # exact ch 2
        chapters = chs((1, 1), (2, 1), (3, 1))
        series = Series(id=1, title="Series", folder_name="vols", alt_titles="")

        scan_series(series, chapters, [vols, chaps])

        # ch 2 points at the precise chapter file, not the volume archive
        assert chapters[1].file_path.endswith("Series ch. 2.cbz")
        assert chapters[0].file_path.endswith("Series v01.cbz")
        assert chapters[2].file_path.endswith("Series v01.cbz")


class TestFindExistingFolder:
    def test_exact_normalized_match(self, tmp_path):
        (tmp_path / "Chainsaw Man").mkdir()
        (tmp_path / "Other").mkdir()
        s = Series(id=1, title="Chainsaw Man!", alt_titles="")
        assert find_existing_folder(tmp_path, s) == "Chainsaw Man"

    def test_alt_title_match(self, tmp_path):
        (tmp_path / "Shingeki no Kyojin").mkdir()
        s = Series(id=1, title="Attack on Titan", alt_titles="Shingeki no Kyojin")
        assert find_existing_folder(tmp_path, s) == "Shingeki no Kyojin"

    def test_no_match(self, tmp_path):
        (tmp_path / "Totally Different").mkdir()
        s = Series(id=1, title="Berserk", alt_titles="")
        assert find_existing_folder(tmp_path, s) is None

    def test_bracketed_decorations_count_as_exact(self, tmp_path):
        (tmp_path / "Berserk (1989)").mkdir()
        s = Series(id=1, title="Berserk", alt_titles="")
        assert find_existing_folder(tmp_path, s) == "Berserk (1989)"

    def test_short_title_does_not_adopt_longer_different_series(self, tmp_path):
        # "Monster" must not adopt another series' folder that merely
        # contains the word — that would mark the wrong files as owned
        (tmp_path / "Monster Musume no Iru Nichijou").mkdir()
        s = Series(id=1, title="Monster", alt_titles="")
        assert find_existing_folder(tmp_path, s) is None

    def test_mostly_same_name_still_loose_matches(self, tmp_path):
        (tmp_path / "One Piece Manga").mkdir()
        s = Series(id=1, title="One Piece", alt_titles="")
        assert find_existing_folder(tmp_path, s) == "One Piece Manga"


def test_scan_repairs_omnibus_mappings_and_clears_false_ownership(tmp_path):
    first = tmp_path / 'Series - Vol. 01.cbz'
    second = tmp_path / 'Series - Vol. 02.cbz'
    with zipfile.ZipFile(first, 'w') as z:
        z.writestr('Series - c016x6 (v02) - p470.png', PNG)
    with zipfile.ZipFile(second, 'w') as z:
        z.writestr('Series - c017 (v03) - p001.jpg', PNG)
        z.writestr('Series - c028x5 (v04) - p441.png', PNG)
    chapters = chs((16.6, 2), (17, 3), (28.5, 4), (5.5, 1))
    # The old scanner assigned original-edition volume 2 to English book 2.
    chapters[0].downloaded = True
    chapters[0].file_path = str(second)
    chapters[3].downloaded = True
    chapters[3].file_path = str(first)
    series = Series(id=1, title='Series', folder_name='Series', alt_titles='')
    result = scan_series(series, chapters, [tmp_path])
    assert result.cleared == 2
    assert chapters[0].file_path == str(first)
    assert chapters[1].file_path == str(second) and chapters[2].file_path == str(second)
    assert not chapters[3].downloaded and chapters[3].file_path == ''
    # A second scan neither changes ownership nor loses the corrected pointers.
    again = scan_series(series, chapters, [tmp_path])
    assert again.cleared == 0 and again.matched_chapters == 0


def test_volume_scan_repoints_stale_path_even_when_old_file_exists_elsewhere(tmp_path):
    scanned = tmp_path / "scanned"
    scanned.mkdir()
    current = scanned / "Series - Vol. 01.cbz"
    make_cbz(current)
    old = tmp_path / "old-name.cbz"
    make_cbz(old)
    chapters = chs((1, 1), (2, 1))
    for chapter in chapters:
        chapter.downloaded = True
        chapter.file_path = str(old)
    series = Series(id=1, title="Series", folder_name="Series", alt_titles="")

    result = scan_series(series, chapters, [scanned])

    assert result.cleared == 0
    assert all(chapter.file_path == str(current) for chapter in chapters)


def test_scan_creates_and_adopts_decimal_chapters_explicitly_named_on_disk(tmp_path):
    make_cbz(tmp_path / "Series - Ch. 0018.1.cbz")
    chapters = chs((18, 3), (19, 3))
    series = Series(id=1, title="Series", folder_name="Series", alt_titles="", monitored=True)
    series.chapters.extend(chapters)

    result = scan_series(series, series.chapters, [tmp_path])

    extra = next(chapter for chapter in series.chapters if chapter.number == 18.1)
    assert result.added_chapters == 1
    assert len(series.chapters) == 3
    assert extra.downloaded and extra.volume == 3
    assert extra.volume_source == "disk-inferred"
    assert extra.file_path.endswith("0018.1.cbz")


def test_title_named_archive_covers_known_single_volume_series(tmp_path):
    archive = tmp_path / "There Are Things I Can't Tell You.cbz"
    make_cbz(archive)
    series = Series(
        id=1, title="There Are Things I Can't Tell You", folder_name="",
        alt_titles="", total_chapters=7, total_volumes=1, monitored=False,
    )

    result = scan_series(series, [], [tmp_path])

    assert result.added_chapters == 7
    assert len(series.chapters) == 7
    assert all(chapter.downloaded and chapter.file_path == str(archive)
               for chapter in series.chapters)


def test_shared_folder_volume_offset_scopes_and_translates_files(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    make_cbz(shared / "Series - Vol. 01.cbz")  # belongs to another part
    wanted = shared / "Series - Vol. 06.cbz"
    make_cbz(wanted)
    chapters = chs((1, 1), (2, 1))
    series = Series(id=1, title="Part 2", folder_name="Part 2", alt_titles="")
    key = str(shared.resolve(strict=False))

    result = scan_series(series, chapters, [shared], {key: 5})

    assert result.volume_files == 1
    assert all(chapter.file_path == str(wanted) for chapter in chapters)


def test_scan_prefers_embedded_combined_number_over_rounded_filename(tmp_path):
    path = tmp_path / "Black Clover - Ch. 0370.4.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.png", PNG)
        archive.writestr(
            "ComicInfo.xml", "<ComicInfo><Number>370.371</Number></ComicInfo>"
        )
    chapter = Chapter(id=1, series_id=1, number=370.371)
    series = Series(id=1, title="Black Clover", folder_name="Black Clover", alt_titles="")
    series.chapters.append(chapter)

    result = scan_series(series, [chapter], [tmp_path])

    assert result.added_chapters == 0
    assert chapter.downloaded and chapter.file_path == str(path)
    assert [item.number for item in series.chapters] == [370.371]
