import zipfile

import pytest

from mangarr.library.matcher import comicinfo_title, find_media_files, match_files
from mangarr.models import Chapter

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def chapters(*specs):
    """specs: (number, volume) tuples."""
    return [Chapter(id=i + 1, series_id=1, number=float(n), volume=v)
            for i, (n, v) in enumerate(specs)]


def touch(path, content=b"x"):
    path.write_bytes(content)


def make_cbz(path, title=""):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("001.png", PNG)
        if title:
            zf.writestr("ComicInfo.xml", f"<ComicInfo><Title>{title}</Title></ComicInfo>")


class TestComicinfoTitle:
    def test_reads_comicinfo_title(self, tmp_path):
        path = tmp_path / "Series - Ch. 001.cbz"
        make_cbz(path, "The Beginning")
        assert comicinfo_title(path) == "The Beginning"

    def test_cached_by_file_version(self, tmp_path):
        path = tmp_path / "Series - Ch. 002.cbz"
        make_cbz(path, "First")
        assert comicinfo_title(path) == "First"
        # rewriting the file (new mtime/size) invalidates the cached title
        make_cbz(path, "Second, Revised")
        import os
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
        assert comicinfo_title(path) == "Second, Revised"


class TestFindMediaFiles:

    def test_parses_varied_real_names(self, tmp_path):
        # names taken from the user's actual library
        for name in [
            "Berserk - Ch. 365.cbz", "Volume 01.cbr", "Assassination Classroom v01.cbr",
            "Dandadan ch. 148.cbz", "Berserk v40 (2019).cbz",
            "Berserk Ch.0364 - A Tear Like Morning Dew.cbz",
        ]:
            touch(tmp_path / name)
        touch(tmp_path / "info.json")  # sidecar ignored

        found = {m.path.name: (m.chapter_number, m.volume_number)
                 for m in find_media_files(tmp_path)}
        assert "info.json" not in found
        assert found["Berserk - Ch. 365.cbz"] == (365.0, None)
        assert found["Volume 01.cbr"] == (None, 1)
        assert found["Assassination Classroom v01.cbr"] == (None, 1)
        assert found["Dandadan ch. 148.cbz"] == (148.0, None)
        assert found["Berserk v40 (2019).cbz"] == (None, 40)
        assert found["Berserk Ch.0364 - A Tear Like Morning Dew.cbz"][0] == 364.0

    def test_loose_image_dir(self, tmp_path):
        d = tmp_path / "Chapter 5"
        d.mkdir()
        touch(d / "01.png", PNG)
        found = find_media_files(tmp_path)
        assert len(found) == 1
        assert found[0].is_dir and found[0].chapter_number == 5.0


class TestMatchFiles:
    def test_chapter_and_volume_coverage(self, tmp_path):
        chs = chapters((1, 1), (2, 1), (3, 1), (10, 2))
        for name in ["Series - Ch. 0003.cbz", "Series v01.cbz", "Random extra.cbz"]:
            touch(tmp_path / name)
        res = match_files(find_media_files(tmp_path), chs)

        by_name = {m.media.path.name: m for m in res.matched}
        # chapter file → single chapter
        assert by_name["Series - Ch. 0003.cbz"].chapter.number == 3.0
        # volume 1 archive covers chapters 1,2,3
        vol = by_name["Series v01.cbz"]
        assert vol.chapter is None and vol.volume == 1
        assert sorted(c.number for c in vol.covered_chapters) == [1.0, 2.0, 3.0]
        # unmatched
        assert [m.path.name for m in res.unmatched] == ["Random extra.cbz"]

    def test_volume_without_tracked_chapters_still_tagged(self, tmp_path):
        chs = chapters((1, 1))
        touch(tmp_path / "Series v09.cbz")
        res = match_files(find_media_files(tmp_path), chs)
        m = res.matched[0]
        assert m.volume == 9 and m.covered_chapters == [] and not res.unmatched
