import zipfile

from mangarr.library.cleanup import analyze, apply_cleanup
from mangarr.library.naming import DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME
from mangarr.models import Chapter, Series

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def make(path, size=1):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("001.png", PNG * size)


def plan(series, chapters, folder):
    return analyze(series, chapters, [folder], DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME)


class TestAnalyze:
    def test_duplicate_volume_keeps_the_in_use_copy(self, tmp_path):
        # the Chained Soldier case: original "v01" (referenced) + torrent "Vol. 01"
        make(tmp_path / "Series v01.cbz")
        make(tmp_path / "Series - Vol. 01.cbz")
        series = Series(id=1, title="Series", folder_name="")
        chs = [Chapter(id=i, series_id=1, number=float(i), volume=1, downloaded=True,
                       file_path=str(tmp_path / "Series v01.cbz")) for i in (1, 2, 3)]
        p = plan(series, chs, tmp_path)

        assert len(p.groups) == 1
        g = p.groups[0]
        assert g.label == "Volume 1"
        keep = [f for f in g.files if f.keep]
        assert len(keep) == 1
        # keep the file already in use; the unreferenced duplicate is removed
        assert keep[0].name == "Series v01.cbz" and keep[0].referenced

    def test_redundant_orphan_default_delete(self, tmp_path):
        # a stray volume file whose chapters are all downloaded elsewhere
        make(tmp_path / "Series - Vol. 05.cbz")
        make(tmp_path / "Series - Ch. 0050.cbz")
        series = Series(id=1, title="Series", folder_name="")
        ch = Chapter(id=1, series_id=1, number=50.0, volume=5, downloaded=True,
                     file_path=str(tmp_path / "Series - Ch. 0050.cbz"))
        p = plan(series, [ch], tmp_path)
        orphan = next(o for o in p.orphans if o.name == "Series - Vol. 05.cbz")
        assert orphan.keep is False  # redundant → default delete

    def test_unknown_extra_default_keep(self, tmp_path):
        make(tmp_path / "Bonus Artbook.cbz")
        series = Series(id=1, title="Series", folder_name="")
        ch = Chapter(id=1, series_id=1, number=1.0, volume=1, downloaded=True,
                     file_path=str(tmp_path / "x"))
        p = plan(series, [ch], tmp_path)
        art = next(o for o in p.orphans if o.name == "Bonus Artbook.cbz")
        assert art.keep is True  # unknown → keep by default


class TestApply:
    def test_deletes_and_repoints_to_survivor(self, tmp_path):
        make(tmp_path / "Series v01.cbz")
        make(tmp_path / "Series - Vol. 01.cbz")
        series = Series(id=1, title="Series", folder_name="")
        chs = [Chapter(id=i, series_id=1, number=float(i), volume=1, downloaded=True,
                       file_path=str(tmp_path / "Series v01.cbz")) for i in (1, 2, 3)]
        # delete the referenced original, keep the canonical → chapters re-point
        res = apply_cleanup(series, chs, [tmp_path], [str(tmp_path / "Series v01.cbz")])
        assert res.deleted == 1
        assert res.repointed == 3
        assert not (tmp_path / "Series v01.cbz").exists()
        assert all(c.file_path.endswith("Series - Vol. 01.cbz") for c in chs)

    def test_refuses_to_delete_last_copy(self, tmp_path):
        make(tmp_path / "Series - Vol. 01.cbz")
        series = Series(id=1, title="Series", folder_name="")
        ch = Chapter(id=1, series_id=1, number=1.0, volume=1, downloaded=True,
                     file_path=str(tmp_path / "Series - Vol. 01.cbz"))
        res = apply_cleanup(series, [ch], [tmp_path], [str(tmp_path / "Series - Vol. 01.cbz")])
        assert res.deleted == 0 and res.skipped == 1
        assert (tmp_path / "Series - Vol. 01.cbz").exists()  # not deleted

    def test_refuses_to_delete_path_outside_series_media(self, tmp_path):
        folder = tmp_path / "Series"
        outside = tmp_path / "outside.cbz"
        folder.mkdir()
        make(folder / "Series - Ch. 0001.cbz")
        make(outside)
        series = Series(id=1, title="Series", folder_name="Series")
        ch = Chapter(id=1, series_id=1, number=1.0, downloaded=True,
                     file_path=str(folder / "Series - Ch. 0001.cbz"))

        res = apply_cleanup(series, [ch], [folder], [str(outside)])

        assert res.deleted == 0 and res.skipped == 1
        assert outside.exists()
