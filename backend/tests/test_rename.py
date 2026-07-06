import zipfile
from pathlib import Path

from mangarr.library.naming import DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME
from mangarr.library.rename import apply_renames, plan_renames
from mangarr.models import Chapter, Series

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def make(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("001.png", PNG)


def plan(series, chapters, folder=None):
    return plan_renames(series, chapters, DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME)


class TestPlanRenames:
    def test_chapter_rename_preserves_extension(self, tmp_path):
        make(tmp_path / "Dandadan ch. 148.cbr")
        series = Series(id=1, title="Dandadan", folder_name="Dandadan")
        ch = Chapter(id=1, series_id=1, number=148.0, volume=None, downloaded=True,
                     file_path=str(tmp_path / "Dandadan ch. 148.cbr"))
        items = plan(series, [ch], tmp_path)
        assert len(items) == 1
        # .cbr preserved, not forced to .cbz
        assert items[0].new_name == "Dandadan - Ch. 0148.cbr"

    def test_already_named_correctly_is_skipped(self, tmp_path):
        make(tmp_path / "Dandadan - Ch. 0148.cbz")
        series = Series(id=1, title="Dandadan", folder_name="Dandadan")
        ch = Chapter(id=1, series_id=1, number=148.0, volume=None, downloaded=True,
                     file_path=str(tmp_path / "Dandadan - Ch. 0148.cbz"))
        assert plan(series, [ch], tmp_path) == []

    def test_volume_archive_with_single_chapter_stays_volume(self, tmp_path):
        # regression: a v13 archive that only one chapter maps to must still be
        # named as a volume, not renamed to that single chapter
        make(tmp_path / "Chained Soldier v13.cbz")
        series = Series(id=1, title="Chained Soldier", folder_name="Chained Soldier")
        fp = str(tmp_path / "Chained Soldier v13.cbz")
        ch = Chapter(id=1, series_id=1, number=106.5, volume=13, downloaded=True, file_path=fp)
        items = plan(series, [ch])
        assert len(items) == 1
        assert items[0].new_name == "Chained Soldier - Vol. 13.cbz"

    def test_volume_file_shared_by_chapters_yields_one_item(self, tmp_path):
        make(tmp_path / "Akira Volume 01.cbz")
        series = Series(id=1, title="Akira", folder_name="Akira")
        fp = str(tmp_path / "Akira Volume 01.cbz")
        chs = [Chapter(id=i, series_id=1, number=float(i), volume=1, downloaded=True,
                       file_path=fp) for i in (1, 2, 3)]
        items = plan(series, chs, tmp_path)
        assert len(items) == 1
        assert items[0].new_name == "Akira - Vol. 01.cbz"
        assert sorted(items[0].chapter_ids) == [1, 2, 3]

    def test_preview_ordered_volumes_then_chapters_numerically(self, tmp_path):
        # volumes 1, 2, 10 then chapters 3, 12, 100 — lexicographic order
        # would give v1, v10, v2 and c100 before c12
        series = Series(id=1, title="My Manga", folder_name="My Manga")
        chapters = []
        for i, vol in enumerate([10, 1, 2], start=1):
            p = tmp_path / f"My Manga v{vol}.cbz"
            make(p)
            chapters.append(Chapter(id=i, series_id=1, number=float(i), volume=vol,
                                    downloaded=True, file_path=str(p)))
        for j, num in enumerate([100, 3, 12], start=10):
            p = tmp_path / f"My Manga c{num}.cbz"
            make(p)
            chapters.append(Chapter(id=j, series_id=1, number=float(num), volume=None,
                                    downloaded=True, file_path=str(p)))
        items = plan(series, chapters, tmp_path)
        assert [i.current_name for i in items] == [
            "My Manga v1.cbz", "My Manga v2.cbz", "My Manga v10.cbz",
            "My Manga c3.cbz", "My Manga c12.cbz", "My Manga c100.cbz",
        ]


class TestApplyRenames:
    def test_moves_and_updates_chapter(self, tmp_path):
        src = tmp_path / "Dandadan ch. 148.cbr"
        make(src)
        series = Series(id=1, title="Dandadan", folder_name="Dandadan")
        ch = Chapter(id=1, series_id=1, number=148.0, volume=None, downloaded=True,
                     file_path=str(src))
        items = plan(series, [ch], tmp_path)
        outcomes = apply_renames(items, {1: ch})
        assert outcomes[0].status == "renamed"
        assert not src.exists()
        assert (tmp_path / "Dandadan - Ch. 0148.cbr").exists()
        assert ch.file_path.endswith("Dandadan - Ch. 0148.cbr")

    def test_renames_in_place_across_directories(self, tmp_path):
        vols = tmp_path / "vols"
        chaps = tmp_path / "chaps"
        vols.mkdir()
        chaps.mkdir()
        vfile = vols / "Series Volume 01.cbz"
        cfile = chaps / "Series ch. 10.cbz"
        make(vfile)
        make(cfile)
        series = Series(id=1, title="Series", folder_name="vols")
        vchs = [Chapter(id=i, series_id=1, number=float(i), volume=1, downloaded=True,
                        file_path=str(vfile)) for i in (1, 2, 3)]
        cch = Chapter(id=10, series_id=1, number=10.0, volume=2, downloaded=True,
                      file_path=str(cfile))
        items = plan(series, [*vchs, cch])
        by_id = {c.id: c for c in [*vchs, cch]}
        apply_renames(items, by_id)

        # volume archive renamed inside the volumes dir; chapter file inside chapters dir
        assert (vols / "Series - Vol. 01.cbz").exists()
        assert (chaps / "Series - Ch. 0010.cbz").exists()
        assert not (chaps / "Series - Vol. 01.cbz").exists()  # not moved across dirs

    def test_collision_is_skipped_not_overwritten(self, tmp_path):
        src = tmp_path / "Dandadan ch. 148.cbr"
        make(src)
        existing = tmp_path / "Dandadan - Ch. 0148.cbr"
        existing.write_bytes(b"keep me")
        series = Series(id=1, title="Dandadan", folder_name="Dandadan")
        ch = Chapter(id=1, series_id=1, number=148.0, volume=None, downloaded=True,
                     file_path=str(src))
        items = plan(series, [ch], tmp_path)
        outcomes = apply_renames(items, {1: ch})
        assert outcomes[0].status == "skipped-collision"
        assert src.exists()  # source untouched
        assert existing.read_bytes() == b"keep me"  # target untouched
