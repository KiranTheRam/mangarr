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
